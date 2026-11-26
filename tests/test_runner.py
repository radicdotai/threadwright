import dataclasses
import json
import os
import stat
import time
from pathlib import Path

import pytest

from threadwright import runner
from threadwright.config import Config

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys, time
mode = {mode!r}
prompt = sys.stdin.read()
if mode == "sleep":
    time.sleep(30)
if mode == "grandchild":
    import subprocess, pathlib
    child = subprocess.Popen(["sleep", "60"])
    pathlib.Path(sys.argv[0] + ".pid").write_text(str(child.pid))
    time.sleep(30)
if mode == "missing":
    print("No conversation found with session ID: sess-0", flush=True)
    print(json.dumps({{"type": "result", "is_error": True, "num_turns": 0, "session_id": "sess-0",
                      "total_cost_usd": 0, "result": ""}}))
    sys.exit(1)
if mode == "crash":
    print("boom: not logged in")
    sys.exit(3)
emit = lambda e: print(json.dumps(e), flush=True)
emit({{"type": "system", "subtype": "init", "session_id": "sess-1"}})
emit({{"type": "assistant", "session_id": "sess-1", "message": {{"content": [
    {{"type": "text", "text": "looking"}},
    {{"type": "tool_use", "name": "Bash", "input": {{"command": "make test", "description": "Run tests"}}}}]}}}})
print("a stray non-json line")
emit({{"type": "result", "subtype": "success", "session_id": "sess-1", "is_error": False,
      "total_cost_usd": 0.42, "result": json.dumps({{"argv": sys.argv[1:], "prompt": prompt}})}})
"""


def alive(pid: int) -> bool:
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        time.sleep(0.1)
    return True


def fake(cfg: Config, tmp_path: Path, mode: str = "ok") -> Config:
    script = tmp_path / f"claude-{mode}"
    script.write_text(FAKE_CLAUDE.format(mode=mode))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return dataclasses.replace(cfg, claude_bin=str(script), task_timeout_s=2)


def test_run_streams_progress_and_returns_result(cfg: Config, tmp_path: Path) -> None:
    steps: list[str] = []
    result = runner.run(fake(cfg, tmp_path), tmp_path, "fix the bug", None, steps.append)
    payload = json.loads(result.text)
    assert payload["prompt"] == "fix the bug"
    assert "--resume" not in payload["argv"]
    assert steps == ["Bash: Run tests"]
    assert result.session_id == "sess-1"
    assert result.cost_usd == 0.42
    assert not result.is_error


def test_run_resumes_existing_session(cfg: Config, tmp_path: Path) -> None:
    result = runner.run(fake(cfg, tmp_path), tmp_path, "and the follow-up", "sess-0", lambda _: None)
    argv = json.loads(result.text)["argv"]
    assert argv[argv.index("--resume") + 1] == "sess-0"


def test_prompt_starting_with_dash_is_not_parsed_as_a_flag(cfg: Config, tmp_path: Path) -> None:
    result = runner.run(fake(cfg, tmp_path), tmp_path, "--dangerously do x", None, lambda _: None)
    assert json.loads(result.text)["prompt"] == "--dangerously do x"


def test_run_times_out(cfg: Config, tmp_path: Path) -> None:
    result = runner.run(fake(cfg, tmp_path, "sleep"), tmp_path, "x", "sess-0", lambda _: None)
    assert result.is_error
    assert result.text.startswith("Stopped after")
    assert result.session_id == "sess-0"


def test_run_without_result_reports_output_tail(cfg: Config, tmp_path: Path) -> None:
    result = runner.run(fake(cfg, tmp_path, "crash"), tmp_path, "x", None, lambda _: None)
    assert result.is_error
    assert "(3)" in result.text
    assert "not logged in" in result.text


def test_missing_session_is_reported_as_resume_failed(cfg: Config, tmp_path: Path) -> None:
    result = runner.run(fake(cfg, tmp_path, "missing"), tmp_path, "x", "sess-0", lambda _: None)
    assert result.is_error and result.resume_failed
    assert "No conversation found" in result.text


def test_ordinary_error_is_not_a_resume_failure(cfg: Config, tmp_path: Path) -> None:
    result = runner.run(fake(cfg, tmp_path, "crash"), tmp_path, "x", "sess-0", lambda _: None)
    assert result.is_error and not result.resume_failed


def test_timeout_kills_the_whole_process_group(cfg: Config, tmp_path: Path) -> None:
    configured = fake(cfg, tmp_path, "grandchild")
    runner.run(configured, tmp_path, "x", None, lambda _: None)
    assert not alive(int(Path(configured.claude_bin + ".pid").read_text()))


def test_failing_progress_callback_stops_claude(cfg: Config, tmp_path: Path) -> None:
    script = tmp_path / "claude-progress"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, subprocess, sys, time\nsys.stdin.read()\n"
        "child = subprocess.Popen(['sleep', '60'])\n"
        "pathlib.Path(sys.argv[0] + '.pid').write_text(str(child.pid))\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash', "
        "'input': {'command': 'ls'}}]}}), flush=True)\ntime.sleep(30)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def boom(_: str) -> None:
        raise RuntimeError("slack down")

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        runner.run(dataclasses.replace(cfg, claude_bin=str(script)), tmp_path, "x", None, boom)
    assert time.monotonic() - started < 5
    assert not alive(int(Path(f"{script}.pid").read_text()))


def test_slack_tokens_never_reach_claude(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("DEV_DB_URL", "postgres://ro@dev")
    script = tmp_path / "claude-env"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\nsys.stdin.read()\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': json.dumps(dict(os.environ))}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    result = runner.run(dataclasses.replace(cfg, claude_bin=str(script)), tmp_path, "x", None, lambda _: None)
    env = json.loads(result.text)
    assert "SLACK_BOT_TOKEN" not in env
    assert env["DEV_DB_URL"] == "postgres://ro@dev"


def test_command_includes_settings_and_mcp(cfg: Config, tmp_path: Path) -> None:
    configured = dataclasses.replace(cfg, claude_settings=tmp_path / "s.json", mcp_config=tmp_path / "m.json")
    cmd = runner.command(configured, None)
    assert cmd[cmd.index("--settings") + 1] == str(tmp_path / "s.json")
    assert cmd[cmd.index("--mcp-config") + 1] == str(tmp_path / "m.json")


def test_command_pins_model_and_effort(cfg: Config) -> None:
    cmd = runner.command(dataclasses.replace(cfg, claude_model="claude-opus-5-5", claude_effort="medium"), None)
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
