import dataclasses
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_listener import BOT, lcfg, make  # noqa: F401
from threadwright import github, workspace
from threadwright.config import ListenerConfig
from threadwright.listener import alert_key, message_text, parse_flags, sweep_due
from threadwright.messages import Update

BRANCH = "threadwright/" + workspace.thread_slug("C", "1.5")


def ci(pr: int = 7) -> github.PrEvent:
    return github.PrEvent("ci", "acme/api", BRANCH, pr, "https://gh/pr/7", "abc1234", "", "ci", "https://gh/run/1")


@pytest.mark.parametrize(("text", "expected"), [
    ("!new fix it", ("fix it", True, False)),
    ("!plan !new add sso", ("add sso", True, True)),
    ("fix !plan later", ("fix !plan later", False, False)),
])
def test_parse_flags(text: str, expected: tuple[str, bool, bool]) -> None:
    assert parse_flags(text) == expected


def test_flags_and_stop_reach_the_worker(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, slack, sent = make(lcfg, [{"ts": "1", "user": "U1", "text": f"<@{BOT}> !plan add sso"}])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "1", "user": "U1", "text": f"<@{BOT}> !plan add sso"})
    assert sent[-1].plan_only and not sent[-1].fresh
    listener.submit({"type": "app_mention", "channel": "C", "ts": "2", "thread_ts": "1", "user": "U1",
                     "text": f"<@{BOT}> stop"})
    assert sent[-1].kind == "stop" and sent[-1].asker == "Asha"
    assert "Stopping" in slack.posted[-1]["text"]


def test_alert_text_comes_from_attachments_and_resolved_alerts_are_skipped(
        lcfg: ListenerConfig) -> None:  # noqa: F811
    message = {"text": "", "attachments": [{"title": "#12 API 5xx", "text": "firing"}]}
    assert message_text(message) == "#12 API 5xx\nfiring"
    listener, _, sent = make(lcfg, [])
    listener.alert({"type": "message", "channel": "A", "ts": "9", "bot_id": "BG", "text": "Resolved: API 5xx"})
    assert sent == []


def test_the_same_alert_starts_one_investigation(lcfg: ListenerConfig) -> None:  # noqa: F811
    root = {"ts": "9", "bot_id": "BG", "username": "Grafana", "text": "#1 API 5xx firing"}
    listener, slack, sent = make(lcfg, [root])
    event = {"type": "message", "channel": "A", "ts": "9", "bot_id": "BG", "username": "Grafana",
             "text": "#1 API 5xx firing"}
    listener.alert(event)
    listener.alert({**event, "ts": "10", "text": "#2 API 5xx firing"})
    assert len(sent) == 1 and sent[0].asker == "Grafana"
    assert sent[0].event.text.startswith("An alert fired")
    assert alert_key("#1 API 5xx firing") == alert_key("#2 API 5xx firing")


def test_watched_posts_are_triaged_then_claimed(lcfg: ListenerConfig, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    listener, slack, sent = make(lcfg, [{"ts": "5", "user": "U2", "text": "checkout is broken"}])
    event = {"type": "message", "channel": "B", "ts": "5", "user": "U2", "text": "checkout is broken"}
    listener.watch(event)
    assert sent[-1].kind == "triage" and sent[-1].status_ts == "" and slack.posted == []
    monkeypatch.setattr(threading, "Thread", lambda target, args=(), daemon=None: type(
        "Now", (), {"start": lambda self: target(*args)})())
    listener.on_update(Update("claim", channel="B", thread_ts="5", event_ts="5"))
    assert sent[-1].kind == "task" and sent[-1].event.text.startswith("This was posted")
    assert slack.posted[-1]["text"] == "Queued…"


def test_skipped_posts_stay_silent(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, slack, sent = make(lcfg, [])
    listener.watch({"type": "message", "channel": "B", "ts": "5", "user": "U2", "text": "lunch?"})
    listener.on_update(Update("skip", channel="B", thread_ts="5", event_ts="5"))
    assert slack.posted == [] and len(sent) == 1


def test_ci_failures_go_to_the_thread_until_the_daily_cap(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, slack, sent = make(dataclasses.replace(lcfg, ci_attempts=2), [])
    listener.on_github(ci())
    assert sent == []
    listener.store.put("thread:C:1.5", "1")
    for _ in range(4):
        listener.on_github(ci())
    assert len(sent) == 2 and all(t.channel == "C" and t.thread_ts == "1.5" for t in sent)
    assert "Attempt 1 of 2" in sent[0].event.text and "Attempt 2 of 2" in sent[1].event.text
    assert sum("limit of 2" in p["text"] for p in slack.posted) == 1


def test_double_clicks_claim_a_card_once(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, _, _ = make(lcfg, [])
    assert listener.store.claim("card:X", "U1") and not listener.store.claim("card:X", "U2")


def test_events_on_other_branches_are_ignored(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, _, sent = make(lcfg, [])
    listener.on_github(dataclasses.replace(ci(), branch="main"))
    assert sent == []


def test_plan_card_approval_starts_the_work_once(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, slack, sent = make(lcfg, [])
    listener.store.add_pending("S0", "C", "1.5")
    listener.on_update(Update("result", "S0", status="Plan ready in 1 min", text="here", plan="1. change x"))
    card = slack.posted[-1]
    assert [e["action_id"] for e in card["blocks"][1]["elements"]] == ["threadwright_plan_approve",
                                                                         "threadwright_plan_change"]
    listener.card_decision("U2", "CARD", "C|1.5", "approve")
    listener.card_decision("U1", "CARD", "C|1.5", "approve")
    assert len(sent) == 1 and "approved the plan" in sent[0].event.text and sent[0].asker == "Ravi"
    assert "Plan approved by <@U2>" in slack.updated[-1]["text"]


def test_guests_cannot_approve(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, _, sent = make(lcfg, [])
    listener.card_decision("G1", "CARD", "C|1.5", "approve")
    assert sent == []


def test_question_card_answer_goes_back_to_the_thread(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, slack, sent = make(lcfg, [])
    listener.store.add_pending("S0", "C", "1.5")
    question = {"question": "Which env?", "options": ["dev", "stage"]}
    listener.on_update(Update("result", "S0", text="q", question=question))
    assert [b["text"]["text"] for b in slack.posted[-1]["blocks"][1]["elements"]] == ["dev", "stage"]
    listener.card_decision("U1", "Q1", "C|1.5", "stage")
    assert sent[-1].event.text == "Asha answered your question: stage"


def test_running_status_has_a_stop_button_and_the_result_drops_it(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, slack, _ = make(lcfg, [])
    listener.store.add_pending("S0", "C", "1.5")
    listener.on_update(Update("progress", "S0", status="Working"))
    assert slack.updated[-1]["blocks"][-1]["elements"][0]["action_id"] == "threadwright_stop"
    listener.on_update(Update("result", "S0", status="Done"))
    assert len(slack.updated[-1]["blocks"]) == 1


def test_sweep_runs_once_a_week_after_its_time() -> None:
    monday = datetime(2026, 11, 30, 5, 0, tzinfo=UTC)
    assert sweep_due(monday, 0, "04:30", None) == "2026-49"
    assert sweep_due(monday, 0, "04:30", "2026-49") is None
    assert sweep_due(monday.replace(hour=4, minute=0), 0, "04:30", None) is None
    assert sweep_due(datetime(2026, 12, 1, 5, 0, tzinfo=UTC), 0, "04:30", None) is None


def test_sweep_posts_and_starts_a_fresh_task(lcfg: ListenerConfig, tmp_path: Path) -> None:  # noqa: F811
    sweep = tmp_path / "sweep.md"
    sweep.write_text("find flaky tests")
    listener, slack, sent = make(dataclasses.replace(lcfg, sweep_channel="A", sweep_prompt=sweep), [])
    listener.maybe_sweep(datetime(2026, 11, 30, 5, 0, tzinfo=UTC))
    listener.maybe_sweep(datetime(2026, 11, 30, 6, 0, tzinfo=UTC))
    assert len(sent) == 1 and sent[0].fresh and sent[0].event.text == "find flaky tests"
    assert slack.posted[0]["text"].startswith("Weekly sweep")


def test_an_alert_storm_with_different_paths_starts_one_task(lcfg: ListenerConfig) -> None:  # noqa: F811
    first = ("*<https://g/a/1|#1605 Unhandled exception: TooManyConnectionsError: remaining connection slots are "
             "reserved - Path: /brands/eb831e43-1f57/guidelines>*")
    second = first.replace("#1605", "#1606").replace("/brands/eb831e43-1f57/guidelines", "/user/me")
    assert alert_key(first) == alert_key(second)


def test_alerts_from_other_bots_and_beyond_the_hourly_cap_are_ignored(lcfg: ListenerConfig) -> None:  # noqa: F811
    listener, _, sent = make(dataclasses.replace(lcfg, alert_bots=frozenset({"BG"}), alerts_per_hour=2), [])
    base = {"type": "message", "channel": "A", "bot_id": "BG", "username": "Grafana"}
    listener.alert({**base, "ts": "1", "bot_id": "BSTATUS", "text": "Incident: degraded"})
    for n, text in enumerate(["db down firing", "api 5xx firing", "queue stuck firing"]):
        listener.alert({**base, "ts": str(n + 2), "text": text})
    assert [t.event.text.splitlines()[-1] for t in sent] == ["db down firing", "api 5xx firing"]


def test_block_kit_alerts_are_read() -> None:
    message = {"text": "", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "#9 Disk full firing"}},
                                      {"type": "context", "elements": [{"type": "mrkdwn", "text": "prod"}]}]}
    assert message_text(message) == "#9 Disk full firing\nprod"
