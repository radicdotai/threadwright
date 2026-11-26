import html
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

_UPLOAD_EVERY_S = 10.0
_POLL_S = 1.0
_OUTPUT_LIMIT = 200_000
_INPUT_LIMIT = 200_000
_REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[redacted]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "[redacted]"),
    (re.compile(r"\bsk[-_](?:live|test|proj)?[-_]?[A-Za-z0-9_\-]{20,}"), "[redacted]"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]+"), "[redacted]"),
    (re.compile(r"xapp-[A-Za-z0-9\-]+"), "[redacted]"),
    (re.compile(r"hooks\.slack\.com/services/\S+"), "hooks.slack.com/services/[redacted]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "[redacted]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "[redacted]"),
    (re.compile(r"\bya29\.[A-Za-z0-9_\-.]+"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[redacted]"),
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_\-.=+/]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?<=://)[^:/@\s]+:[^@\s]+(?=@)"), "[redacted]"),
    (re.compile(r"""(?i)(\b[\w.\-]*(?:pass(?:word|wd)?|secret|token|api[_\-]?key|private[_\-]?key|credential)s?\w*"""
                r"""["']?\s*[=:]\s*["']?)[^\s"',;]{6,}"""), r"\1[redacted]"),
]

_STYLE = """
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:960px;margin:24px auto;
padding:0 16px;color:#1d1d1f;background:#fff}
h1{font-size:18px;margin:0 0 4px}.meta{color:#6e6e73;margin-bottom:20px}
.ev{border-left:3px solid #d2d2d7;padding:6px 12px;margin:10px 0}.ask{border-color:#0071e3}
.say{border-color:#34c759}.tool{border-color:#ff9f0a}.done{border-color:#34c759;background:#f2fbf4}
.fail{border-color:#ff3b30;background:#fff2f1}.label{font-weight:600;font-size:12px;color:#6e6e73}
pre{white-space:pre-wrap;word-break:break-word;background:#f5f5f7;padding:8px;border-radius:6px;margin:4px 0;
font:12px/1.45 ui-monospace,Menlo,monospace}summary{cursor:pointer;color:#6e6e73;font-size:12px}
.output-error summary{color:#ff3b30}.think summary{font-style:italic}.meta-ev,.ev.meta{border-color:#af52de}
"""


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"


def _clean(text: str) -> str:
    return html.escape(redact(text))


def _diff(old: str, new: str) -> str:
    return "\n".join([*(f"- {line}" for line in old.splitlines()), *(f"+ {line}" for line in new.splitlines())])


def _tool_text(name: str, inputs: Any) -> str:
    if not isinstance(inputs, dict):
        return str(inputs)
    if name == "Bash":
        head = f"# {inputs['description']}\n" if inputs.get("description") else ""
        return head + str(inputs.get("command", ""))
    if name == "Edit":
        return f"{inputs.get('file_path', '')}\n\n" + _diff(str(inputs.get("old_string", "")),
                                                          str(inputs.get("new_string", "")))
    if name == "MultiEdit":
        edits = inputs.get("edits", [])
        parts = [_diff(str(e.get("old_string", "")), str(e.get("new_string", "")))
                 for e in edits if isinstance(e, dict)]
        return f"{inputs.get('file_path', '')}\n\n" + "\n\n".join(parts)
    if name == "Write":
        return f"{inputs.get('file_path', '')}\n\n{inputs.get('content', '')}"
    if name == "TodoWrite":
        marks = {"completed": "[x]", "in_progress": "[~]"}
        return "\n".join(f"{marks.get(str(t.get('status')), '[ ]')} {t.get('content', '')}"
                         for t in inputs.get("todos", []) if isinstance(t, dict))
    return json.dumps(inputs, indent=2, ensure_ascii=False)


def _tool_output(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) if part.get("type") != "image" else "[image]"
                         for part in content if isinstance(part, dict))
    if isinstance(content, (dict, list)):
        return json.dumps(content, indent=2, ensure_ascii=False)
    return str(content or "")


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content", []) if isinstance(message, dict) else []
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def compact(event: dict[str, Any]) -> list[tuple[str, str, str]]:
    kind = event.get("type")
    entries: list[tuple[str, str, str]] = []
    if kind == "system" and event.get("subtype") == "init":
        entries.append(("meta", "Session", f"model: {event.get('model', '?')}\nfolder: {event.get('cwd', '?')}"))
    elif kind == "assistant":
        for block in _blocks(event):
            kind_of = block.get("type")
            if kind_of == "text" and str(block.get("text", "")).strip():
                entries.append(("say", "Claude", str(block["text"]).strip()))
            elif kind_of == "thinking" and str(block.get("thinking", "")).strip():
                entries.append(("think", "Thinking", _cap(str(block["thinking"]).strip(), _INPUT_LIMIT)))
            elif kind_of == "tool_use":
                name = str(block.get("name", "tool"))
                entries.append(("tool", name, _cap(_tool_text(name, block.get("input", {})), _INPUT_LIMIT)))
    elif kind == "user":
        for block in _blocks(event):
            if block.get("type") == "tool_result":
                output = _tool_output(block)
                css = "output-error" if block.get("is_error") else "output"
                entries.append((css, f"{'error' if block.get('is_error') else 'output'} ({len(output)} characters)",
                                _cap(output, _OUTPUT_LIMIT)))
    elif kind == "result":
        cost, seconds = _number(event.get("total_cost_usd")), _number(event.get("duration_ms")) / 1000
        css = "fail" if event.get("is_error") else "done"
        label = f"Final answer · ${cost:.2f} · {seconds:.0f}s · {int(_number(event.get('num_turns')))} turns"
        entries.append((css, label, str(event.get("result") or "").strip()))
    return entries


def _entry_html(css: str, label: str, text: str) -> str:
    if css in ("output", "output-error", "think"):
        return (f'<details class="{css}"><summary>{_clean(label)}</summary>'
                f"<pre>{_clean(text)}</pre></details>")
    return f'<div class="ev {css}"><div class="label">{_clean(label)}</div><pre>{_clean(text)}</pre></div>'


def render(title: str, request: str, entries: list[tuple[str, str, str]], state: str,
           refresh_url: str | None) -> str:
    final = next((text for css, _, text in reversed(entries) if css in ("done", "fail")), "")
    shown = [e for e in entries if not (e[0] == "say" and final and e[2] == final)]
    refresh = f'<meta http-equiv="refresh" content="10;url={html.escape(refresh_url)}">' if refresh_url else ""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = "".join(_entry_html(*e) for e in shown)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{_clean(title)}</title>{refresh}'
            f"<style>{_STYLE}</style></head><body><h1>{_clean(title)}</h1>"
            f'<div class="meta">{html.escape(state)} · updated {stamp}</div>'
            f'<div class="ev ask"><div class="label">Request</div><pre>{_clean(request)}</pre></div>'
            f"{body}</body></html>")


class SessionPage:
    def __init__(self, url: str, title: str, request: str, upload: Callable[[str], None],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.url = url
        self._title = title
        self._request = request
        self._upload = upload
        self._clock = clock
        self._entries: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()
        self._dirty = False
        self._last_upload = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _push(self, state: str, live: bool) -> bool:
        try:
            with self._lock:
                page = render(self._title, self._request, list(self._entries), state, self.url if live else None)
                self._dirty = False
                self._last_upload = self._clock()
            self._upload(page)
            return True
        except Exception:
            log.exception("could not upload session page")
            return False

    def start(self) -> bool:
        if not self._push("Running", live=True):
            return False
        self._thread.start()
        return True

    def on_event(self, event: dict[str, Any]) -> None:
        try:
            entries = compact(event)
        except Exception:
            log.exception("could not read a session event")
            return
        if entries:
            with self._lock:
                self._entries.extend(entries)
                self._dirty = True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._dirty = True

    def flush_if_due(self) -> None:
        with self._lock:
            due = self._dirty and self._clock() - self._last_upload >= _UPLOAD_EVERY_S
        if due:
            self._push("Running", live=True)

    def _loop(self) -> None:
        while not self._stop.wait(_POLL_S):
            self.flush_if_due()

    def finish(self, failed: bool) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        self._push("Failed" if failed else "Done", live=False)
