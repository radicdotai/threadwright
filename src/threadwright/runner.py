import contextlib
import json
import os
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from threadwright.config import Config

_MISSING_SESSION = "No conversation found"


@dataclass(frozen=True)
class Result:
    text: str
    session_id: str | None
    cost_usd: float
    is_error: bool
    resume_failed: bool = False


def command(cfg: Config, session_id: str | None) -> list[str]:
    cmd = [cfg.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions"]
    if cfg.claude_model:
        cmd += ["--model", cfg.claude_model]
    if cfg.claude_effort:
        cmd += ["--effort", cfg.claude_effort]
    if session_id:
        cmd += ["--resume", session_id]
    if cfg.claude_settings:
        cmd += ["--settings", str(cfg.claude_settings)]
    if cfg.mcp_config:
        cmd += ["--mcp-config", str(cfg.mcp_config)]
    return cmd


def child_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not k.startswith("SLACK_")}


def parse(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def describe(event: dict[str, Any]) -> str | None:
    if event.get("type") != "assistant":
        return None
    for block in event.get("message", {}).get("content", []):
        if block.get("type") != "tool_use":
            continue
        inputs = block.get("input", {})
        detail = inputs.get("description") or inputs.get("command") or inputs.get("file_path") or inputs.get("pattern")
        tool = block.get("name", "tool")
        return f"{tool}: {str(detail)[:120]}" if detail else str(tool)
    return None


def _kill_group(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def run(cfg: Config, cwd: Path, prompt: str, session_id: str | None,
        on_progress: Callable[[str], None], on_event: Callable[[dict[str, Any]], None] | None = None) -> Result:
    proc = subprocess.Popen(command(cfg, session_id), cwd=cwd, env=child_env(dict(os.environ)),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        _kill_group(proc)

    timer = threading.Timer(cfg.task_timeout_s, expire)
    timer.start()
    latest_session = session_id
    result: dict[str, Any] | None = None
    noise: deque[str] = deque(maxlen=10)
    try:
        assert proc.stdin and proc.stdout
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        for line in proc.stdout:
            event = parse(line)
            if event is None:
                noise.append(line.strip())
                continue
            latest_session = event.get("session_id") or latest_session
            if on_event:
                on_event(event)
            if event.get("type") == "result":
                result = event
            elif step := describe(event):
                on_progress(step)
        proc.wait()
    finally:
        timer.cancel()
        _kill_group(proc)
        proc.wait()
    if timed_out.is_set():
        return Result(f"Stopped after {cfg.task_timeout_s // 60} minutes without finishing.",
                      latest_session, 0.0, True)
    tail = " ".join(line for line in noise if line)[-500:]
    if result is None:
        return Result(f"Claude exited ({proc.returncode}) without a result. {tail}".strip(),
                      latest_session, 0.0, True)
    is_error = bool(result.get("is_error"))
    resume_failed = bool(session_id) and is_error and not result.get("num_turns") and _MISSING_SESSION in tail
    return Result(str(result.get("result") or tail), latest_session,
                  float(result.get("total_cost_usd") or 0.0), is_error, resume_failed)
