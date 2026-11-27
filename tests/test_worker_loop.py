import dataclasses
import json
import os
import stat
from pathlib import Path

from tests.test_worker import setup, task
from threadwright import runner
from threadwright.config import Config
from threadwright.session import add_secrets, redact
from threadwright.worker import take_question, triage

FAKE = """#!/usr/bin/env python3
import json, os, pathlib, sys
prompt = json.loads(sys.stdin.readline())["message"]["content"]
pathlib.Path(".threadwright").mkdir(exist_ok=True)
if "PLANME" in prompt:
    pathlib.Path(".threadwright/plan.md").write_text("1. change x")
if "ASKME" in prompt:
    q = {"question": "Which env?", "options": ["dev", "stage"]}
    pathlib.Path(".threadwright/question.json").write_text(json.dumps(q))
env = {k: os.environ.get(k, "") for k in ("GIT_AUTHOR_NAME", "HTTPS_PROXY")}
env["gh"] = os.environ.get("GH_TOKEN") == "ghs_abc"
out = json.dumps({"prompt": prompt, "env": env})
print(json.dumps({"type": "result", "is_error": False, "session_id": "s", "result": out}), flush=True)
"""


def worker_with(cfg: Config, tmp_path: Path, script: str = FAKE):  # type: ignore[no-untyped-def]
    worker, updates, store = setup(cfg, tmp_path)
    fake = tmp_path / "claude-loop"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    worker.cfg = dataclasses.replace(worker.cfg, claude_bin=str(fake))
    return worker, updates, store


def test_plan_file_becomes_a_plan_and_plan_only_says_so(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = worker_with(cfg, tmp_path)
    worker.handle(dataclasses.replace(task("2", "add sso PLANME"), plan_only=True))
    result = updates[-1]
    assert result.plan == "1. change x" and result.status.startswith("Plan ready")
    assert json.loads(result.text)["prompt"].startswith("Plan only")


def test_question_file_becomes_a_card(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = worker_with(cfg, tmp_path)
    worker.handle(task("2", "ASKME"))
    assert updates[-1].question == {"question": "Which env?", "options": ["dev", "stage"]}


def test_bad_question_files_are_dropped(tmp_path: Path) -> None:
    (tmp_path / ".threadwright").mkdir()
    (tmp_path / ".threadwright" / "question.json").write_text("not json")
    assert take_question(tmp_path) == {}
    assert not (tmp_path / ".threadwright" / "question.json").exists()


def test_fresh_starts_a_new_session(cfg: Config, tmp_path: Path) -> None:
    worker, _, store = worker_with(cfg, tmp_path)
    store.save_session("C:1", "old")
    worker.handle(dataclasses.replace(task("2", "start over"), fresh=True))
    assert store.session("C:1") == "s"


def test_github_app_token_git_identity_and_proxy_reach_claude(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = worker_with(cfg, tmp_path)

    class Tokens:
        def token(self) -> str:
            return "ghs_abc"

    worker.tokens = Tokens()
    worker.cfg = dataclasses.replace(worker.cfg, git_name="threadwright[bot]", egress_proxy="http://127.0.0.1:8888")
    worker.handle(task("2", "go"))
    env = json.loads(updates[-1].text)["env"]
    assert env == {"gh": True, "GIT_AUTHOR_NAME": "threadwright[bot]", "HTTPS_PROXY": "http://127.0.0.1:8888"}


def test_a_message_for_a_busy_thread_joins_the_running_task(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = worker_with(cfg, tmp_path)
    sent: list[str] = []

    class Running(runner.Run):
        def send(self, text: str) -> bool:
            sent.append(text)
            return True

    worker._runs["C:1"] = Running()
    worker.handle(task("3", "also stage"))
    assert "also stage" in sent[0] and updates[-1].status.startswith("Added to the task")


def test_stop_reaches_the_running_task(cfg: Config, tmp_path: Path) -> None:
    worker, updates, _ = worker_with(cfg, tmp_path)
    stopped: list[str] = []

    class Running(runner.Run):
        def stop(self, who: str) -> bool:
            stopped.append(who)
            return True

    worker._runs["C:1"] = Running()
    worker.handle(dataclasses.replace(task("4", "stop"), kind="stop"))
    assert stopped == ["Asha"] and updates[-1].status == "Stopping."


def test_triage_says_yes_only_on_a_clear_yes_and_fails_closed(cfg: Config, tmp_path: Path) -> None:
    for answer, expected in (("YES", True), ("no", False), ("Yes, clearly.", True)):
        fake = tmp_path / f"claude-{expected}-{len(answer)}"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\nsys.stdin.read()\n"
                        f"print(json.dumps({{'result': {answer!r}}}))\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        assert triage(dataclasses.replace(cfg, claude_bin=str(fake)), "checkout broken") is expected
    assert triage(dataclasses.replace(cfg, claude_bin=str(tmp_path / "missing")), "x") is False


def test_triage_task_claims_or_skips(cfg: Config, tmp_path: Path) -> None:
    fake = tmp_path / "claude-yes"
    fake.write_text("#!/usr/bin/env python3\nimport json,sys\nsys.stdin.read()\nprint(json.dumps({'result': 'YES'}))\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    worker, updates, _ = worker_with(cfg, tmp_path)
    worker.cfg = dataclasses.replace(worker.cfg, claude_bin=str(fake))
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    worker.handle(dataclasses.replace(task("6", "checkout broken"), kind="triage", status_ts=""))
    assert (updates[-1].kind, updates[-1].channel, updates[-1].event_ts) == ("claim", "C", "6")


def test_literal_secrets_from_the_environment_are_masked() -> None:
    add_secrets({"GRAFANA_TOKEN": "glsa_plainvalue123", "DEV_DB_URL": "postgres://u:p@h/db",
                 "SENTRY_URL": "https://de.sentry.io", "GITHUB_APP_KEY": "/etc/key.pem"})
    out = redact("token glsa_plainvalue123 db postgres://u:p@h/db see https://de.sentry.io")
    assert "glsa_plainvalue123" not in out and "u:p@" not in out and "https://de.sentry.io" in out
    os.environ.pop("UNUSED", None)
