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
from typing import IO, Any

from threadwright.config import Config

_MISSING_SESSION = "No conversation found"
NUDGE_AFTER = 4
STOP_AFTER = 7
NUDGE = ("You have run the same step {n} times in a row without progress. Change approach, or stop and report "
         "where you are stuck and what you need.")


@dataclass(frozen=True)
class Result:
    text: str
    session_id: str | None
    cost_usd: float
    is_error: bool
    resume_failed: bool = False


def command(cfg: Config, session_id: str | None) -> list[str]:
    cmd = [cfg.claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
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


def child_env(env: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    return {**{k: v for k, v in env.items() if not k.startswith("SLACK_")}, **(extra or {})}


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


def step_key(event: dict[str, Any]) -> str | None:
    if event.get("type") != "assistant":
        return None
    calls = [json.dumps([b.get("name"), b.get("input")], sort_keys=True)
             for b in event.get("message", {}).get("content", []) if b.get("type") == "tool_use"]
    return "|".join(calls) or None


def user_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def _kill_group(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


class Run:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stdin: IO[str] | None = None
        self._proc: subprocess.Popen[str] | None = None
        self.stopped_by = ""

    def attach(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._proc, self._stdin = proc, proc.stdin

    def send(self, text: str) -> bool:
        with self._lock:
            if self._stdin is None:
                return False
            try:
                self._stdin.write(user_line(text))
                self._stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                self._stdin = None
                return False
            return True

    def close_input(self) -> None:
        with self._lock:
            stdin, self._stdin = self._stdin, None
        if stdin is not None:
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                stdin.close()

    def stop(self, who: str) -> bool:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            self.stopped_by = who
        _kill_group(proc)
        return True


def run(cfg: Config, cwd: Path, prompt: str, session_id: str | None,
        on_progress: Callable[[str], None], on_event: Callable[[dict[str, Any]], None] | None = None,
        handle: Run | None = None, env: dict[str, str] | None = None) -> Result:
    handle = handle or Run()
    proc = subprocess.Popen(command(cfg, session_id), cwd=cwd, env=child_env(dict(os.environ), env),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    handle.attach(proc)
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        _kill_group(proc)

    timer = threading.Timer(cfg.task_timeout_s, expire)
    timer.start()
    latest_session = session_id
    results: list[dict[str, Any]] = []
    noise: deque[str] = deque(maxlen=10)
    last_step, repeats, nudged, stuck = "", 0, False, False
    try:
        assert proc.stdout
        handle.send(prompt)
        for line in proc.stdout:
            event = parse(line)
            if event is None:
                noise.append(line.strip())
                continue
            latest_session = event.get("session_id") or latest_session
            if on_event:
                on_event(event)
            if event.get("type") == "result":
                results.append(event)
                handle.close_input()
                continue
            if key := step_key(event):
                repeats = repeats + 1 if key == last_step else 1
                last_step = key
                if repeats >= NUDGE_AFTER and not nudged:
                    nudged = handle.send(NUDGE.format(n=repeats))
                elif repeats >= STOP_AFTER:
                    stuck = True
                    _kill_group(proc)
            if step := describe(event):
                on_progress(step)
        proc.wait()
    finally:
        timer.cancel()
        handle.close_input()
        _kill_group(proc)
        proc.wait()
    cost = sum(float(r.get("total_cost_usd") or 0.0) for r in results)
    done = "\n\n".join(str(r.get("result") or "") for r in results if r.get("result"))
    if handle.stopped_by:
        return Result(f"Stopped by {handle.stopped_by}. Work so far is kept on the branch.\n\n{done}".strip(),
                      latest_session, cost, True)
    if stuck:
        return Result(f"Stopped: I repeated the same step {repeats} times without progress.\n\n{done}".strip(),
                      latest_session, cost, True)
    if timed_out.is_set():
        return Result(f"Stopped after {cfg.task_timeout_s // 60} minutes without finishing.",
                      latest_session, cost, True)
    tail = " ".join(line for line in noise if line)[-500:]
    if not results:
        return Result(f"Claude exited ({proc.returncode}) without a result. {tail}".strip(),
                      latest_session, 0.0, True)
    last = results[-1]
    is_error = bool(last.get("is_error"))
    resume_failed = bool(session_id) and is_error and not last.get("num_turns") and _MISSING_SESSION in tail
    return Result(done or tail, latest_session, cost, is_error, resume_failed)
