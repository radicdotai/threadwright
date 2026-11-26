from typing import Any

import pytest

from threadwright.session import SessionPage, compact, redact, render

SECRETS = [
    "sk-ant-oat01-AbCdEf_123-xyz",
    "sk-proj-" + "a" * 30,
    "sk_live_" + "b" * 24,
    "xoxb-1234-5678-abcdEFGH",
    "xapp-1-A0CL-123-abc",
    "ghp_" + "a" * 36,
    "github_pat_11ABC_def",
    "AIza" + "B" * 35,
    "ya29.a0AfH6SMBx-token_value",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Q",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_tokens_are_redacted(secret: str) -> None:
    out = redact(f"value {secret} end")
    assert secret not in out and "[redacted]" in out


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("postgres://reader:s3cretPW@10.0.0.5:5432/db", "postgres://[redacted]@10.0.0.5:5432/db"),
        ("host=db user=ro password=s3cretPW dbname=x", "password=[redacted]"),
        ("PGPASSWORD=s3cretPW psql", "PGPASSWORD=[redacted]"),
        ("STRIPE_SECRET_KEY=abcdef123456", "STRIPE_SECRET_KEY=[redacted]"),
        ('"private_key_id": "0123456789abcdef"', '"private_key_id": "[redacted]'),
        ("Authorization: Bearer abcdefgh12345678", "Bearer [redacted]"),
        ("https://hooks.slack.com/services/T0/B0/xyz", "hooks.slack.com/services/[redacted]"),
    ],
)
def test_credentials_in_config_lines_are_redacted(text: str, kept: str) -> None:
    out = redact(text)
    assert "s3cretPW" not in out and "abcdef123456" not in out and kept in out


def test_half_a_private_key_is_still_redacted() -> None:
    out = redact("key:\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC")
    assert "MIIE" not in out


def events() -> list[dict[str, Any]]:
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking at <script>alert(1)</script>"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "make test", "description": "Run tests"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "token xoxb-1-2-abc\n" + "x" * 250_000}]}},
        {"type": "result", "is_error": False, "total_cost_usd": 0.42, "result": "Fixed it"},
    ]


def entries() -> list[tuple[str, str, str]]:
    return [entry for event in events() for entry in compact(event)]


def test_render_escapes_redacts_caps_and_refreshes_only_while_live() -> None:
    live = render("Asha · fix login", "please fix", entries(), "Running", "https://storage.cloud.google.com/b/s.html")
    assert "<script>" not in live and "&lt;script&gt;" in live
    assert "xoxb-1-2-abc" not in live and "[redacted]" in live
    assert "more characters)" in live and "x" * 200_100 not in live
    assert "make test" in live and "Run tests" in live and "Fixed it" in live and "$0.42" in live
    assert 'http-equiv="refresh" content="10;url=https://storage.cloud.google.com/b/s.html"' in live
    assert "http-equiv" not in render("t", "r", entries(), "Done", None)


def test_compact_keeps_tool_output_bounded() -> None:
    output = [e for e in entries() if e[0] == "output"][0]
    assert len(output[2]) < 200_100


@pytest.mark.parametrize(
    "event",
    [
        {"type": "assistant", "message": {"content": ["not a block", 3]}},
        {"type": "assistant", "message": "oops"},
        {"type": "result", "total_cost_usd": "n/a", "result": None},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": {"weird": 1}}]}},
    ],
)
def test_malformed_events_never_raise(event: dict[str, Any]) -> None:
    page = SessionPage("u", "t", "r", lambda _: None)
    page.on_event(event)
    page.finish(failed=False)


def test_final_answer_is_not_shown_twice() -> None:
    said = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed it"}]}}
    rows = compact(said) + compact({"type": "result", "result": "Fixed it", "total_cost_usd": 0.1})
    assert render("t", "r", rows, "Done", None).count("Fixed it") == 1


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_events_never_upload_inline_and_flush_waits_10s() -> None:
    clock, pages = Clock(), list[str]()
    page = SessionPage("https://x/s.html", "title", "request", pages.append, clock=clock)
    assert page._push("Running", live=True)
    for event in events():
        page.on_event(event)
    assert len(pages) == 1
    clock.now = 9
    page.flush_if_due()
    assert len(pages) == 1
    clock.now = 10
    page.flush_if_due()
    assert len(pages) == 2 and "Fixed it" in pages[1] and "http-equiv" in pages[1]
    page.flush_if_due()
    assert len(pages) == 2
    page.finish(failed=False)
    assert len(pages) == 3 and "Done" in pages[2] and "http-equiv" not in pages[2]


def test_failed_start_reports_false_and_clear_drops_old_run() -> None:
    def broken(_: str) -> None:
        raise OSError("gcs down")

    assert SessionPage("u", "t", "r", broken).start() is False
    pages: list[str] = []
    page = SessionPage("u", "t", "r", pages.append)
    for event in events():
        page.on_event(event)
    page.clear()
    page.finish(failed=False)
    assert "Fixed it" not in pages[-1]


def test_every_kind_of_step_is_shown_in_full() -> None:
    rows = compact({"type": "system", "subtype": "init", "model": "claude-x", "cwd": "/srv/w"})
    rows += compact({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "plan: check the guard"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "app/a.py", "old_string": "a - b",
                                                       "new_string": "a + b"}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": "t.py", "content": "def test(): pass"}},
        {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
            {"content": "Fix add", "status": "completed"}, {"content": "Run tests", "status": "in_progress"}]}},
        {"type": "tool_use", "name": "Task", "input": {"description": "review", "prompt": "Review the diff"}}]}})
    rows += compact({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "Traceback boom"}]}]}})
    rows += compact({"type": "result", "result": "ok", "total_cost_usd": 1.5, "duration_ms": 90_000, "num_turns": 7})
    page = render("t", "r", rows, "Done", None)
    for expected in ("claude-x", "/srv/w", "plan: check the guard", "- a - b", "+ a + b", "def test(): pass",
                     "[x] Fix add", "[~] Run tests", "Review the diff", "Traceback boom", "output-error",
                     "$1.50 · 90s · 7 turns"):
        assert expected in page, expected
