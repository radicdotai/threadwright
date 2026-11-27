import base64
import dataclasses
import json
import stat
from pathlib import Path

import pytest

from tests.test_workspace import make_mirror
from threadwright.config import Config
from threadwright.messages import SlackFile, SlackMessage, Task, Update
from threadwright.store import Store
from threadwright.worker import PublishError, StatusTicker, Worker, elapsed_text

FAKE = """#!/usr/bin/env python3
import json, sys
prompt = json.loads(sys.stdin.readline())["message"]["content"]
print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-9"}), flush=True)
print(json.dumps({"type": "result", "is_error": False, "session_id": "sess-9", "total_cost_usd": 0.25,
                  "result": json.dumps({"argv": sys.argv[1:], "prompt": prompt})}), flush=True)
"""


def setup(cfg: Config, tmp_path: Path) -> tuple[Worker, list[Update], Store]:
    for name in cfg.repos:
        make_mirror(cfg, name, tmp_path)
    script = tmp_path / "claude-fake"
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    store = Store(cfg.db_path)
    updates: list[Update] = []
    return Worker(dataclasses.replace(cfg, claude_bin=str(script)), store, updates.append), updates, store


def task(event_ts: str, text: str, files: list[SlackFile] | None = None) -> Task:
    event = SlackMessage("Asha", text, files or [])
    return Task(f"C:{event_ts}", "C", "1", f"S{event_ts}", "Asha", event,
                [SlackMessage("Ravi", "login broken", [SlackFile("shot.png", base64.b64encode(b"png").decode())]),
                 event])


def payload(update: Update) -> dict[str, list[str] | str]:
    data: dict[str, list[str] | str] = json.loads(update.text)
    return data


def test_first_task_sends_whole_thread_then_follow_up_resumes(cfg: Config, tmp_path: Path) -> None:
    worker, updates, store = setup(cfg, tmp_path)
    worker.handle(task("2", "please fix"))
    first = payload(updates[-1])
    assert updates[-1].kind == "result" and updates[-1].status.startswith("Done in")
    assert "[Ravi]: login broken" in first["prompt"] and "--resume" not in first["argv"]
    shot = next(Path(cfg.work_dir).rglob("*shot.png"))
    assert shot.read_bytes() == b"png"
    worker.handle(task("3", "also check stage"))
    second = payload(updates[-1])
    assert "also check stage" in second["prompt"] and "login broken" not in second["prompt"]
    assert second["argv"][second["argv"].index("--resume") + 1] == "sess-9"
    assert store.session("C:1") == "sess-9"
    assert worker.busy == 0


def test_redelivered_task_runs_once_and_clears_its_status(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    worker.handle(task("2", "fix"))
    worker.handle(task("2", "fix"))
    results = [u for u in updates if u.kind == "result"]
    assert results[0].status.startswith("Done in") and results[1].status == "Already handled."


def test_unpublishable_result_leaves_the_task_for_redelivery(cfg: Config, tmp_path: Path) -> None:
    worker, _, store = setup(cfg, tmp_path)

    def failing(update: Update) -> None:
        if update.kind == "result":
            raise PublishError("down")

    worker.publish = failing
    with pytest.raises(PublishError):
        worker.handle(task("2", "fix"))
    assert not store.is_done("C:2")


def test_budget_stops_before_running(cfg: Config, tmp_path: Path) -> None:
    worker, updates, store = setup(cfg, tmp_path)
    worker.cfg = dataclasses.replace(worker.cfg, monthly_budget_usd=0.1)
    worker.handle(task("2", "fix"))
    worker.handle(task("3", "fix again"))
    assert updates[-1].status == "This month's budget is used up."


def test_failure_is_reported_and_task_is_not_retried_forever(cfg: Config, tmp_path: Path) -> None:
    worker, updates, store = setup(cfg, tmp_path)
    worker.cfg = dataclasses.replace(worker.cfg, repos=("missing-repo",))
    worker.handle(task("2", "fix"))
    assert updates[-1].kind == "result" and updates[-1].status == "Failed before finishing"
    assert store.is_done("C:2")


STEPPING = """#!/usr/bin/env python3
import json, sys
sys.stdin.readline()
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Bash", "input": {"command": "make test"}}]}}), flush=True)
print(json.dumps({"type": "result", "is_error": False, "session_id": "s", "total_cost_usd": 0, "result": "ok"}),
      flush=True)
"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def ticker(clock: FakeClock) -> tuple[StatusTicker, list[str]]:
    sent: list[str] = []
    return StatusTicker(task("2", "fix"), lambda u: sent.append(u.status), clock), sent


def test_elapsed_text() -> None:
    assert elapsed_text(7.9) == "7s" and elapsed_text(80) == "1m 20s" and elapsed_text(3605) == "60m 05s"


def test_status_starts_with_thinking_and_ends_with_a_done_marker(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    worker.handle(task("2", "fix"))
    assert (updates[0].kind, updates[0].status) == ("progress", "Thinking…")
    assert updates[-1].event_ts == "2" and not updates[-1].failed


def test_first_step_is_shown_immediately(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    (tmp_path / "claude-fake").write_text(STEPPING)
    worker.handle(task("2", "fix"))
    statuses = [u.status for u in updates if u.kind == "progress"]
    assert statuses[0] == "Thinking…"
    assert statuses[1].startswith("◐ Working · ") and statuses[1].endswith(" · Bash: make test")


def test_ticker_shows_elapsed_time_while_thinking_and_refreshes_every_10s() -> None:
    clock = FakeClock()
    status, sent = ticker(clock)
    with status:
        clock.now += 9
        status.tick()
        assert sent == ["Thinking…"]
        clock.now += 1
        status.tick()
        assert sent[-1] == "◐ Thinking · 10s"
        clock.now += 70
        status.tick()
        assert sent[-1] == "◓ Thinking · 1m 20s"


def test_ticker_refresh_keeps_the_latest_step_and_rotates_the_spinner() -> None:
    clock = FakeClock()
    status, sent = ticker(clock)
    status.step("Bash: make test")
    assert sent == ["◐ Working · 0s · Bash: make test"]
    clock.now += 3
    status.step("Read: a.py")
    assert len(sent) == 1
    clock.now += 7
    status.tick()
    clock.now += 10
    status.tick()
    assert sent[1:] == ["◓ Working · 10s · Read: a.py", "◑ Working · 20s · Read: a.py"]


def test_later_steps_are_throttled_to_five_seconds() -> None:
    clock = FakeClock()
    status, sent = ticker(clock)
    status.step("a")
    clock.now += 5
    status.step("b")
    assert [s.rsplit(" · ", 1)[1] for s in sent] == ["a", "b"]


def test_thinking_status_covers_workspace_preparation(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    worker.cfg = dataclasses.replace(worker.cfg, repos=("missing-repo",))
    worker.handle(task("2", "fix"))
    assert updates[0].status == "Thinking…"
    assert updates[-1].status == "Failed before finishing"


def test_already_handled_marks_the_message_failed(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    worker.handle(task("2", "fix"))
    worker.handle(task("2", "fix"))
    assert updates[-1].status == "Already handled."
    assert updates[-1].failed and updates[-1].event_ts == "2"


def test_failed_run_is_flagged(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    worker.cfg = dataclasses.replace(worker.cfg, repos=("missing-repo",))
    worker.handle(task("2", "fix"))
    assert updates[-1].failed and updates[-1].event_ts == "2"


def test_session_link_rides_every_update_and_page_holds_the_run(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    pages: dict[str, list[str]] = {}
    worker.cfg = dataclasses.replace(worker.cfg, sessions_bucket="threadwright-sessions")
    worker.upload = lambda name, html: pages.setdefault(name, []).append(html)
    worker.handle(task("2", "please fix login"))
    url = "https://storage.cloud.google.com/threadwright-sessions/sessions/C/1/2.html"
    assert {u.session_url for u in updates if u.kind in ("progress", "result")} == {url}
    final = pages["sessions/C/1/2.html"][-1]
    assert "please fix login" in final and "Asha · please fix login" in final
    assert "Done" in final and "http-equiv" not in final


def test_no_bucket_means_no_link(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    worker.upload = lambda name, html: None
    worker.handle(task("2", "fix"))
    assert all(u.session_url == "" for u in updates)


def test_failed_upload_means_no_link(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = setup(cfg, tmp_path)
    worker.cfg = dataclasses.replace(worker.cfg, sessions_bucket="threadwright-sessions")

    def broken(name: str, html: str) -> None:
        raise OSError("403")

    worker.upload = broken
    worker.handle(task("2", "fix"))
    assert all(u.session_url == "" for u in updates)
    assert updates[-1].status.startswith("Done in")


def test_budget_refusal_gets_no_page(cfg: Config, tmp_path: Path) -> None:
    worker, updates, store = setup(cfg, tmp_path)
    pages: list[str] = []
    worker.cfg = dataclasses.replace(worker.cfg, sessions_bucket="threadwright-sessions", monthly_budget_usd=0.1)
    worker.upload = lambda name, html: pages.append(name)
    worker.handle(task("2", "fix"))
    pages.clear()
    worker.handle(task("3", "again"))
    assert updates[-1].status == "This month's budget is used up." and pages == []


def test_status_step_text_is_redacted() -> None:
    sent: list[Update] = []
    ticker = StatusTicker(task("2", "x"), sent.append)
    ticker.step("Bash: DB_PASSWORD=hunter2secret psql postgres://ro:pw12345@db/x")
    status = sent[-1].status
    assert "hunter2secret" not in status and "pw12345" not in status and "[redacted]" in status
