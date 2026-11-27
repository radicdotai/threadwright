import base64
import json
import logging
import re
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from threadwright import actions, prompt
from threadwright.config import ListenerConfig
from threadwright.messages import SlackFile, SlackMessage, Task, Update, decode_update, encode
from threadwright.store import Store
from threadwright.vm import GceInstance, WorkerVm

log = logging.getLogger(__name__)

_MAX_FILE_BYTES = 6 * 1024 * 1024
_HANDLED_SUBTYPES = {None, "file_share", "thread_broadcast"}
_MAX_THREAD_MESSAGES = 200
_CHANNEL_CONTEXT = 30
_TICK_S = 30.0
_MAX_HOLD_MIN = 240
FENCE = "`" * 3
_HOLD = re.compile(r"^hold\s+(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hour|hours)?$", re.I)
_RELEASE = re.compile(r"^(hold\s+off|release)$", re.I)


def parse_hold(text: str) -> int | None:
    text = text.strip()
    if _RELEASE.match(text):
        return 0
    match = _HOLD.match(text)
    if not match:
        return None
    unit = (match.group(2) or "m").lower()
    minutes = int(match.group(1)) * (60 if unit.startswith("h") else 1)
    return min(max(minutes, 1), _MAX_HOLD_MIN)
_SLACK_ATTEMPTS = 5
_RATE_LIMITED = 429


def thread_key(event: dict[str, Any]) -> tuple[str, str]:
    return event["channel"], event.get("thread_ts") or event["ts"]


def should_run(event: dict[str, Any], bot_user_id: str) -> bool:
    if event.get("subtype") not in _HANDLED_SUBTYPES or event.get("bot_id") or event.get("user") in (None, bot_user_id):
        return False
    return event.get("type") == "app_mention" or event.get("channel_type") == "im"


def may_use(profile: dict[str, Any], team_id: str, allowed_users: frozenset[str]) -> bool:
    if allowed_users and profile.get("id") not in allowed_users:
        return False
    if profile.get("is_bot") or profile.get("deleted"):
        return False
    if profile.get("is_restricted") or profile.get("is_ultra_restricted"):
        return False
    return profile.get("team_id") == team_id


class Listener:
    def __init__(self, cfg: ListenerConfig, store: Store, client: WebClient, bot_user_id: str, team_id: str,
                 publish_task: Callable[[Task], None], vm: WorkerVm, bot_id: str | None = None,
                 targets: actions.Targets | None = None,
                 sign: Callable[[str, bytes], bytes] | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client
        self.bot_user_id = bot_user_id
        self.bot_id = bot_id
        self.sleep: Callable[[float], None] = time.sleep
        self.team_id = team_id
        self.publish_task = publish_task
        self.vm = vm
        self.profiles: dict[str, dict[str, Any]] = {}
        self.seen: set[str] = set()
        self._submit_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self.targets = targets
        self.sign = sign

    def profile(self, user_id: str) -> dict[str, Any]:
        if user_id not in self.profiles:
            self.profiles[user_id] = dict(self.client.users_info(user=user_id)["user"])
        return self.profiles[user_id]

    def name(self, user_id: str | None) -> str:
        if not user_id:
            return "bot"
        p = self.profile(user_id)
        return str(p.get("real_name") or p.get("name") or user_id)

    def _files(self, message: dict[str, Any], budget: list[int]) -> list[SlackFile]:
        files: list[SlackFile] = []
        for f in message.get("files", []):
            url, size = f.get("url_private_download"), int(f.get("size", 0))
            if not url or size > budget[0]:
                continue
            auth = {"Authorization": f"Bearer {self.cfg.slack_bot_token}"}
            with urllib.request.urlopen(urllib.request.Request(url, headers=auth), timeout=60) as response:
                data = response.read()
            budget[0] -= len(data)
            files.append(SlackFile(Path(f.get("name") or "file").name, base64.b64encode(data).decode()))
        return files

    def _message(self, message: dict[str, Any], budget: list[int]) -> SlackMessage:
        text = prompt.strip_mention(message.get("text", ""), self.bot_user_id)
        return SlackMessage(self.name(message.get("user")), text, self._files(message, budget))

    def _is_own(self, message: dict[str, Any]) -> bool:
        return message.get("user") == self.bot_user_id or (
            self.bot_id is not None and message.get("bot_id") == self.bot_id)

    def _history(self, channel: str, ts: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.client.conversations_replies(channel=channel, ts=ts, limit=200, cursor=cursor)
            messages.extend(page["messages"])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                replies = [m for m in messages[1:] if not self._is_own(m)]
                return messages[:1] + replies[-(_MAX_THREAD_MESSAGES - 1):]

    def _recent(self, channel: str, ts: str) -> list[SlackMessage]:
        try:
            page = self.client.conversations_history(channel=channel, latest=ts, inclusive=False,
                                                     limit=_CHANNEL_CONTEXT)
        except Exception:
            log.warning("could not read recent messages in %s", channel)
            return []
        return [SlackMessage(self.name(m.get("user")), m.get("text", ""))
                for m in reversed(page["messages"]) if not self._is_own(m)]

    def build_task(self, event: dict[str, Any], status_ts: str) -> Task:
        channel, ts = thread_key(event)
        history = self._history(channel, ts)
        budget = [_MAX_FILE_BYTES]
        current = self._message(event, budget)
        thread = [current if m.get("ts") == event["ts"] else self._message(m, budget) for m in history]
        if not any(m.get("ts") == event["ts"] for m in history):
            thread.append(current)
        recent = [] if event.get("thread_ts") else self._recent(channel, ts)
        return Task(f"{channel}:{event['ts']}", channel, ts, status_ts, self.name(event["user"]), current, thread,
                    recent)

    def submit(self, event: dict[str, Any]) -> None:
        channel, ts = thread_key(event)
        event_id = f"{channel}:{event['ts']}"
        with self._submit_lock:
            if event_id in self.seen:
                return
            self.seen.add(event_id)
        if not may_use(self.profile(event["user"]), self.team_id, self.cfg.allowed_users):
            self.client.chat_postMessage(channel=channel, thread_ts=ts,
                                         text="Only members of this Slack workspace can use me.")
            return
        if self.cfg.allowed_channels and event.get("channel_type") != "im" \
                and channel not in self.cfg.allowed_channels:
            self.client.chat_postMessage(channel=channel, thread_ts=ts, text="I'm not enabled in this channel.")
            return
        hold = parse_hold(prompt.strip_mention(event.get("text", ""), self.bot_user_id))
        if hold is not None:
            self._hold(channel, ts, hold)
            return
        try:
            self.client.reactions_add(channel=channel, timestamp=event["ts"], name="eyes")
        except Exception:
            log.warning("could not react to %s", event_id)
        status = self.client.chat_postMessage(channel=channel, thread_ts=ts, text="Queued…")["ts"]
        self.store.add_pending(status, channel, ts)
        try:
            self.publish_task(self.build_task(event, status))
        except Exception as exc:
            log.exception("could not queue %s", event_id)
            self.store.remove_pending(status)
            self.client.chat_update(channel=channel, ts=status, text=f"Could not queue this: {exc}")
            self._finish_reaction(channel, event["ts"], failed=True)
            return
        self.vm.touch()
        threading.Thread(target=self._wake, args=(channel, status, event["ts"]), daemon=True).start()

    def _hold(self, channel: str, ts: str, minutes: int) -> None:
        if minutes == 0:
            self.vm.release()
            self.client.chat_postMessage(channel=channel, thread_ts=ts,
                                         text="Hold released. The worker sleeps after 15 idle minutes.")
            return
        self.vm.hold(minutes * 60)
        until = time.strftime("%H:%M UTC", time.gmtime(time.time() + minutes * 60))
        note = self.client.chat_postMessage(channel=channel, thread_ts=ts,
                                            text=f"Holding the worker awake until {until} ({minutes} min). "
                                                 "Say `hold off` to release.")["ts"]
        threading.Thread(target=self._wake_for_hold, args=(channel, note, until, minutes), daemon=True).start()

    def _wake_for_hold(self, channel: str, note: str, until: str, minutes: int) -> None:
        try:
            if self.vm.wake():
                self.client.chat_update(channel=channel, ts=note,
                                        text=f"Starting the worker (1–3 min); it stays awake until {until} "
                                             f"({minutes} min). Say `hold off` to release.")
        except Exception as exc:
            log.exception("could not wake the worker for a hold")
            self.client.chat_update(channel=channel, ts=note, text=f"Could not start the worker: {exc}")

    def _wake(self, channel: str, status: str, event_ts: str) -> None:
        try:
            if self.vm.wake():
                self.client.chat_update(channel=channel, ts=status, text="Queued, starting the worker (1–3 min)…")
        except Exception as exc:
            log.exception("could not wake the worker")
            self.client.chat_update(channel=channel, ts=status, text=f"Could not start the worker: {exc}")
            self._finish_reaction(channel, event_ts, failed=True)

    def on_update(self, update: Update) -> None:
        with self._update_lock:
            self._apply(update)

    def _apply(self, update: Update) -> None:
        if update.kind == "heartbeat":
            self.vm.heartbeat(update.busy)
            return
        target = self.store.pending(update.status_ts)
        if target is None:
            return
        self.vm.touch()
        channel, ts = target
        text = update.status or "Working…"
        if update.session_url:
            text = f"{text} · <{update.session_url}|View session>"
        if update.kind != "result":
            self.client.chat_update(channel=channel, ts=update.status_ts, text=text)
            return
        self._retrying(self.client.chat_update, channel=channel, ts=update.status_ts, text=text)
        self.store.remove_pending(update.status_ts)
        if update.text:
            for part in prompt.chunks(prompt.to_mrkdwn(update.text)):
                self._retrying(self.client.chat_postMessage, channel=channel, thread_ts=ts, text=part)
        if update.actions:
            self._propose(channel, ts, update.actions)
        if update.event_ts:
            self._finish_reaction(channel, update.event_ts, failed=update.failed)

    def _propose(self, channel: str, ts: str, raw: list[dict[str, Any]]) -> None:
        if self.targets is None:
            self._retrying(self.client.chat_postMessage, channel=channel, thread_ts=ts,
                           text="I proposed changes, but approvals are not set up here.")
            return
        try:
            proposed = actions.parse(json.dumps(raw), set(self.targets.envs))
        except ValueError as exc:
            self._retrying(self.client.chat_postMessage, channel=channel, thread_ts=ts,
                           text=f"Proposed changes were refused: {exc}")
            return
        proposal_id = str(uuid.uuid4())
        try:
            card = self.client.chat_postMessage(channel=channel, thread_ts=ts, text=_card_text(proposed),
                                                blocks=_card(proposed, self.targets.approvers, proposal_id))["ts"]
        except Exception as exc:
            log.exception("could not post proposal in %s:%s", channel, ts)
            self._retrying(self.client.chat_postMessage, channel=channel, thread_ts=ts,
                           text=f"Could not post the proposed changes: {exc}")
            return
        self.store.add_proposal(proposal_id, channel, ts, card, json.dumps(actions.to_dicts(proposed)))
        log.info("proposal %s in %s:%s: %s", proposal_id, channel, ts, raw)

    def decide(self, user: str, proposal_id: str, approve: bool) -> None:
        if self.targets is None or self.sign is None:
            return
        if user not in self.targets.approvers:
            claimed = None
        else:
            claimed = self.store.decide(proposal_id, "running" if approve else "rejected", user, actions.MAX_AGE_S)
        if claimed is None:
            log.warning("refused decision on %s by %s", proposal_id, user)
            return
        channel, ts, card, raw = claimed
        status, summary = "failed", "could not run"
        try:
            status, summary = self._run_decision(user, proposal_id, approve, channel, ts, card,
                                                 actions.from_dicts(json.loads(raw)))
        except Exception as exc:
            log.exception("proposal %s failed", proposal_id)
            summary = f"could not run: {exc}"
            self._retrying(self.client.chat_postMessage, channel=channel, thread_ts=ts,
                           text=f"Approved changes stopped with an error: {exc}. Check before retrying.")
        finally:
            self.store.finish_proposal(proposal_id, status, summary)

    def _run_decision(self, user: str, proposal_id: str, approve: bool, channel: str, ts: str, card: str,
                      proposed: list[actions.Action]) -> tuple[str, str]:
        assert self.targets is not None and self.sign is not None
        if not approve:
            self.client.chat_update(channel=channel, ts=card, text=f"Rejected by <@{user}>.",
                                    blocks=_card(proposed, None, proposal_id, f"Rejected by <@{user}>."))
            return "rejected", ""
        self.client.chat_update(channel=channel, ts=card, text=f"Approved by <@{user}>, running…",
                                blocks=_card(proposed, None, proposal_id, f"Approved by <@{user}>, running…"))
        email = str((self.client.users_info(user=user)["user"].get("profile") or {}).get("email") or "")
        outcomes = actions.execute(proposed, self.targets, email, self.sign)
        ok = len(outcomes) == len(proposed) and all(o.ok for o in outcomes)
        summary = _results(outcomes, len(proposed))
        log.info("proposal %s approved by %s: %s", proposal_id, user, "done" if ok else "failed")
        verdict = f"{'Done' if ok else 'Stopped'} · approved by <@{user}>"
        self.client.chat_update(channel=channel, ts=card, text=verdict,
                                blocks=_card(proposed, None, proposal_id, verdict))
        for part in prompt.chunks(summary):
            self._retrying(self.client.chat_postMessage, channel=channel, thread_ts=ts, text=part)
        return "done" if ok else "failed", summary

    def recover(self) -> None:
        for proposal_id, channel, ts in self.store.interrupted():
            log.warning("proposal %s was interrupted", proposal_id)
            self._retrying(self.client.chat_postMessage, channel=channel, thread_ts=ts,
                           text="Approved changes were interrupted by a restart. Some may have run; check before "
                                "asking again.")

    def _retrying(self, call: Callable[..., Any], **kwargs: Any) -> None:
        for attempt in range(1, _SLACK_ATTEMPTS + 1):
            try:
                call(**kwargs)
                return
            except SlackApiError as exc:
                if exc.response.status_code != _RATE_LIMITED or attempt == _SLACK_ATTEMPTS:
                    raise
                self.sleep(float(exc.response.headers.get("Retry-After", 1)))

    def _finish_reaction(self, channel: str, event_ts: str, failed: bool) -> None:
        try:
            self.client.reactions_remove(channel=channel, timestamp=event_ts, name="eyes")
        except Exception:
            log.warning("could not remove :eyes: from %s:%s", channel, event_ts)
        try:
            self.client.reactions_add(channel=channel, timestamp=event_ts, name="x" if failed else "white_check_mark")
        except Exception:
            log.warning("could not add the final reaction to %s:%s", channel, event_ts)


def _card_text(proposed: list[actions.Action]) -> str:
    return f"Approval needed: {len(proposed)} proposed change(s)."


def _card(proposed: list[actions.Action], approvers: frozenset[str] | None, proposal_id: str,
          verdict: str = "") -> list[dict[str, Any]]:
    title = {"type": "mrkdwn", "text": f"*{_card_text(proposed)}*"}
    blocks: list[dict[str, Any]] = [{"type": "section", "text": title}]
    for n, a in enumerate(proposed, 1):
        call = {"type": "mrkdwn", "text": f"{n}. `{a.method} {a.path}` on *{a.env}*"}
        blocks.append({"type": "section", "text": call})
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": f"Why: {a.why}", "emoji": False}})
        if a.body is not None:
            body = {"type": "plain_text", "text": actions.show_body(a.body), "emoji": False}
            blocks.append({"type": "section", "text": body})
    if approvers is None:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": verdict}]})
        return blocks
    who = ", ".join(f"<@{u}>" for u in sorted(approvers))
    note = {"type": "mrkdwn", "text": f"Can approve: {who}. Expires in 24 h."}
    blocks.append({"type": "context", "elements": [note]})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "action_id": "threadwright_approve", "style": "primary", "value": proposal_id,
         "text": {"type": "plain_text", "text": "Approve"},
         "confirm": {"title": {"type": "plain_text", "text": "Run these changes?"},
                     "text": {"type": "mrkdwn", "text": "They run as you, with your access."},
                     "confirm": {"type": "plain_text", "text": "Run"},
                     "deny": {"type": "plain_text", "text": "Cancel"}}},
        {"type": "button", "action_id": "threadwright_reject", "style": "danger", "value": proposal_id,
         "text": {"type": "plain_text", "text": "Reject"}},
    ]})
    return blocks


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(FENCE, "ʼʼʼ")


def _results(outcomes: list[actions.Outcome], total: int) -> str:
    lines = []
    for n, o in enumerate(outcomes, 1):
        mark = "✅" if o.ok else "❌"
        call = f"`{o.action.method} {o.action.path}` on {o.action.env}"
        lines.append(f"{mark} {n}. {call} → {o.status or 'no response'}\n```{_escape(o.body)}```")
    if len(outcomes) < total:
        lines.append(f"Skipped the remaining {total - len(outcomes)} change(s).")
    return "\n".join(lines)


def run(cfg: ListenerConfig) -> None:
    import google.cloud.pubsub_v1 as pubsub_v1
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=cfg.slack_bot_token)
    identity = app.client.auth_test()
    publisher = pubsub_v1.PublisherClient()
    topic = publisher.topic_path(cfg.project, cfg.tasks_topic)

    def publish_task(task: Task) -> None:
        publisher.publish(topic, encode(task)).result(timeout=60)

    vm = WorkerVm(GceInstance(cfg.project, cfg.zone, cfg.worker_instance), cfg.idle_stop_s, cfg.idle_action,
                  cfg.max_busy_s)
    targets = actions.load_targets(cfg.actions_config) if cfg.actions_config else None
    sign = actions.kms_signer() if targets else None
    listener = Listener(cfg, Store(cfg.db_path), app.client, identity["user_id"], identity["team_id"],
                        publish_task, vm, identity.get("bot_id"), targets, sign)

    def on_event(event: dict[str, Any]) -> None:
        if should_run(event, listener.bot_user_id):
            listener.submit(event)

    app.event("app_mention")(on_event)
    app.event("message")(on_event)

    def on_decision(ack: Callable[[], None], body: dict[str, Any]) -> None:
        ack()
        action = body["actions"][0]
        listener.decide(body["user"]["id"], action["value"], action["action_id"] == "threadwright_approve")

    listener.recover()
    app.action("threadwright_approve")(on_decision)
    app.action("threadwright_reject")(on_decision)

    def on_message(message: Any) -> None:
        try:
            listener.on_update(decode_update(message.data))
        except Exception:
            log.exception("could not apply update")
        message.ack()

    subscriber = pubsub_v1.SubscriberClient()
    subscriber.subscribe(subscriber.subscription_path(cfg.project, cfg.updates_subscription), on_message)

    def idle_loop() -> None:
        while True:
            time.sleep(_TICK_S)
            try:
                vm.tick()
            except Exception:
                log.exception("idle check failed")

    threading.Thread(target=idle_loop, daemon=True).start()
    SocketModeHandler(app, cfg.slack_app_token).start()
