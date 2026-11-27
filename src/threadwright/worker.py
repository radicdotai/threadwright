import base64
import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from threadwright import actions, prompt, runner, workspace
from threadwright.config import Config
from threadwright.messages import SlackFile, Task, Update, decode_task, encode
from threadwright.session import SessionPage, redact
from threadwright.store import Store

log = logging.getLogger(__name__)

_PROGRESS_EVERY_S = 5.0
_REFRESH_S = 10.0
_SPINNER = "◐◓◑◒"
_RESULT_ATTEMPTS = 5


_HEARTBEAT_S = 60.0
_TITLE_CHARS = 90


class PublishError(RuntimeError):
    pass


def elapsed_text(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


class StatusTicker:
    def __init__(self, task: Task, publish: Callable[[Update], None],
                 clock: Callable[[], float] = time.monotonic, session_url: str = "") -> None:
        self._task = task
        self._session_url = session_url
        self._publish = publish
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._frame = 0
        self._step = ""
        self._last_sent: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _send(self) -> None:
        now = self._clock()
        with self._lock:
            frame = _SPINNER[self._frame % len(_SPINNER)]
            self._frame += 1
            self._last_sent = now
            label = f"Working · {elapsed_text(now - self._started)} · {self._step}" if self._step \
                else f"Thinking · {elapsed_text(now - self._started)}"
        self._publish(Update("progress", self._task.status_ts, status=f"{frame} {label}",
                             session_url=self._session_url))

    def step(self, step: str) -> None:
        with self._lock:
            first = not self._step
            self._step = redact(step)
            due = first or self._last_sent is None or self._clock() - self._last_sent >= _PROGRESS_EVERY_S
        if due:
            self._send()

    def tick(self) -> None:
        with self._lock:
            due = self._last_sent is None or self._clock() - self._last_sent >= _REFRESH_S
        if due:
            self._send()

    def _loop(self) -> None:
        while not self._stop.wait(_REFRESH_S / 4):
            try:
                self.tick()
            except Exception:
                log.exception("could not refresh status")

    def __enter__(self) -> "StatusTicker":
        self._publish(Update("progress", self._task.status_ts, status="Thinking…", session_url=self._session_url))
        with self._lock:
            self._last_sent = self._clock()
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()


def month_start() -> float:
    now = datetime.now(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC).timestamp()


def save_files(files: list[SlackFile], dest: Path) -> list[Path]:
    saved: list[Path] = []
    for index, f in enumerate(files):
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{index}-{Path(f.name).name or 'file'}"
        path.write_bytes(base64.b64decode(f.data))
        saved.append(path)
    return saved


def _event_ts(task: Task) -> str:
    return task.event_id.rpartition(":")[2]


def session_object(task: Task) -> str:
    return f"sessions/{task.channel}/{task.thread_ts}/{_event_ts(task)}.html"


def session_url(bucket: str, task: Task) -> str:
    return f"https://storage.cloud.google.com/{bucket}/{session_object(task)}"


class Worker:
    def __init__(self, cfg: Config, store: Store, publish: Callable[[Update], None],
                 upload: Callable[[str, str], None] | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.publish = publish
        self.upload = upload
        self.busy = 0
        self._busy_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextmanager
    def _working(self) -> Iterator[None]:
        with self._busy_lock:
            self.busy += 1
        try:
            yield
        finally:
            with self._busy_lock:
                self.busy -= 1

    def handle(self, task: Task) -> None:
        if self.store.is_done(task.event_id):
            self.publish(Update("result", task.status_ts, status="Already handled.",
                                event_ts=_event_ts(task), failed=True))
            return
        key = f"{task.channel}:{task.thread_ts}"
        with self._working(), self._locks.setdefault(key, threading.Lock()):
            try:
                self._work(task, key)
            except PublishError:
                raise
            except Exception as exc:
                log.exception("task failed for %s", key)
                self.publish(Update("result", task.status_ts, status="Failed before finishing", text=str(exc),
                                    event_ts=_event_ts(task), failed=True))
            self.store.mark_done(task.event_id)

    def _full_prompt(self, task: Task, root: Path) -> str:
        files = save_files([f for m in task.thread for f in m.files], root / "slack-files")
        return prompt.initial(task.asker, [(m.name, m.text) for m in task.thread], files,
                              [(m.name, m.text) for m in task.channel_recent])

    def _page(self, task: Task) -> SessionPage | None:
        if not self.upload or not self.cfg.sessions_bucket:
            return None
        upload, name = self.upload, session_object(task)
        first_line = (task.event.text.strip().splitlines() or ["(no text)"])[0]
        page = SessionPage(session_url(self.cfg.sessions_bucket, task), f"{task.asker} · {first_line[:_TITLE_CHARS]}",
                           task.event.text, lambda html: upload(name, html))
        return page if page.start() else None

    def _work(self, task: Task, key: str) -> None:
        budget = self.cfg.monthly_budget_usd
        if budget is not None and self.store.spent_since(month_start()) >= budget:
            self.publish(Update("result", task.status_ts, status="This month's budget is used up.",
                                event_ts=_event_ts(task), failed=True))
            return
        page = self._page(task)
        url = page.url if page else ""
        with StatusTicker(task, self.publish, session_url=url) as ticker:
            update = self._run(task, key, ticker, page)
        if page:
            page.finish(update.failed)
        self.publish(dataclasses.replace(update, session_url=url))

    def _run(self, task: Task, key: str, ticker: StatusTicker, page: SessionPage | None) -> Update:
        root = workspace.prepare(self.cfg, workspace.thread_slug(task.channel, task.thread_ts))
        session = self.store.session(key)
        if session:
            files = save_files(task.event.files, root / "slack-files")
            text = prompt.follow_up(task.asker, task.event.text, files)
        else:
            text = self._full_prompt(task, root)

        started = time.time()
        on_event = page.on_event if page else None
        result = runner.run(self.cfg, root, text, session, ticker.step, on_event)
        if result.resume_failed:
            if page:
                page.clear()
            result = runner.run(self.cfg, root, self._full_prompt(task, root), None, ticker.step, on_event)
        if result.session_id:
            self.store.save_session(key, result.session_id)
        self.store.log_run(key, task.asker, started, result.cost_usd, result.is_error)
        outcome = "Failed" if result.is_error else "Done"
        minutes = (time.time() - started) / 60
        proposed, problem = actions.take(root, set(self.cfg.action_envs))
        text = "\n\n".join(part for part in (result.text or "(no reply)", problem) if part)
        return Update("result", task.status_ts, status=f"{outcome} in {minutes:.0f} min", text=text,
                      event_ts=_event_ts(task), failed=result.is_error, actions=actions.to_dicts(proposed))


def run(cfg: Config) -> None:
    import google.cloud.pubsub_v1 as pubsub_v1
    from google.cloud.pubsub_v1.subscriber.scheduler import ThreadScheduler

    publisher = pubsub_v1.PublisherClient()
    topic = publisher.topic_path(cfg.project, cfg.updates_topic)

    def publish(update: Update) -> None:
        attempts = _RESULT_ATTEMPTS if update.kind == "result" else 1
        for attempt in range(1, attempts + 1):
            try:
                publisher.publish(topic, encode(update)).result(timeout=60)
                return
            except Exception:
                log.exception("could not publish %s update (attempt %s)", update.kind, attempt)
                time.sleep(min(2 ** attempt, 30))
        if update.kind == "result":
            raise PublishError("result could not be published")

    upload: Callable[[str, str], None] | None = None
    if cfg.sessions_bucket:
        import google.cloud.storage as storage

        bucket = storage.Client(project=cfg.project).bucket(cfg.sessions_bucket)

        def upload(name: str, html: str) -> None:
            blob = bucket.blob(name)
            blob.cache_control = "no-store"
            blob.upload_from_string(html, content_type="text/html; charset=utf-8", timeout=15)

    worker = Worker(cfg, Store(cfg.db_path), publish, upload)

    def heartbeat() -> None:
        while True:
            publish(Update("heartbeat", busy=worker.busy))
            time.sleep(_HEARTBEAT_S)

    threading.Thread(target=heartbeat, daemon=True).start()

    def on_task(message: Any) -> None:
        try:
            task = decode_task(message.data)
        except Exception:
            log.exception("dropping undecodable task")
            message.ack()
            return
        try:
            worker.handle(task)
        except PublishError:
            message.nack()
            return
        message.ack()

    subscriber = pubsub_v1.SubscriberClient()
    flow = pubsub_v1.types.FlowControl(max_messages=cfg.max_concurrent,
                                       max_lease_duration=cfg.task_timeout_s * 2 + 900)
    scheduler = ThreadScheduler(ThreadPoolExecutor(max_workers=cfg.max_concurrent))
    future = subscriber.subscribe(subscriber.subscription_path(cfg.project, cfg.tasks_subscription),
                                  on_task, flow_control=flow, scheduler=scheduler)
    log.info("worker pulling %s", cfg.tasks_subscription)
    future.result()
