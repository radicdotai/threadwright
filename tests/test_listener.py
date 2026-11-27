from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from threadwright.config import ListenerConfig
from threadwright.listener import Listener, parse_hold
from threadwright.messages import Task, Update, decode_task, encode
from threadwright.store import Store

BOT = "UBOT"


class FakeSlack:
    def __init__(self, history: list[dict[str, Any]]) -> None:
        self.history = history
        self.channel: list[dict[str, Any]] = []
        self.history_reads = 0
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.update_failures: list[Exception] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.users: dict[str, dict[str, Any]] = {"U1": {"id": "U1", "team_id": "T1", "real_name": "Asha"},
                      "U2": {"id": "U2", "team_id": "T1", "real_name": "Ravi"},
                      "UBOT": {"id": "UBOT", "team_id": "T1", "is_bot": True, "name": "factory"},
                      "G1": {"id": "G1", "team_id": "T1", "is_restricted": True}}

    def users_info(self, user: str) -> dict[str, Any]:
        return {"user": self.users[user]}

    def conversations_replies(self, channel: str, ts: str, limit: int, cursor: str | None = None) -> dict[str, Any]:
        if self.history and self.history[0].get("text") == "boom":
            raise RuntimeError("ratelimited")
        start = int(cursor or 0)
        page = self.history[start:start + 2]
        more = start + 2 < len(self.history)
        return {"messages": page, "response_metadata": {"next_cursor": str(start + 2) if more else ""}}

    def conversations_history(self, channel: str, latest: str, inclusive: bool, limit: int) -> dict[str, Any]:
        self.history_reads += 1
        return {"messages": [m for m in reversed(self.channel) if float(m["ts"]) < float(latest)][:limit]}

    def reactions_add(self, **kwargs: Any) -> None:
        self.reactions.append(("add", kwargs["timestamp"], kwargs["name"]))

    def reactions_remove(self, **kwargs: Any) -> None:
        self.reactions.append(("remove", kwargs["timestamp"], kwargs["name"]))

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ts": f"S{len(self.posted)}"}

    def chat_update(self, **kwargs: Any) -> None:
        if self.update_failures:
            raise self.update_failures.pop(0)
        self.updated.append(kwargs)


class FakeResponse:
    def __init__(self, retry_after: str) -> None:
        self.status_code = 429
        self.headers = {"Retry-After": retry_after}


def RateLimited(retry_after: str) -> SlackApiError:
    return SlackApiError("ratelimited", FakeResponse(retry_after))  # type: ignore[no-untyped-call]


class FakeVm:
    def __init__(self) -> None:
        self.touched = 0
        self.beats: list[int] = []
        self.wake_error: Exception | None = None
        self.holds: list[int] = []

    def touch(self) -> None:
        self.touched += 1

    def heartbeat(self, busy: int) -> None:
        self.beats.append(busy)

    def wake(self) -> bool:
        if self.wake_error:
            raise self.wake_error
        return False

    def hold(self, seconds: int) -> None:
        self.holds.append(seconds)

    def release(self) -> None:
        self.holds.append(0)


@pytest.fixture
def lcfg(tmp_path: Path) -> ListenerConfig:
    return ListenerConfig("xoxb", "xapp", "p", "z", "threadwright-worker", "t", "s", tmp_path / "l.db", 900, "stop",
                          7200, frozenset(), frozenset())


def make(lcfg: ListenerConfig, history: list[dict[str, Any]]) -> tuple[Listener, FakeSlack, list[Task]]:
    slack, sent = FakeSlack(history), list[Task]()
    listener = Listener(lcfg, Store(lcfg.db_path), slack, BOT, "T1", sent.append, FakeVm())  # type: ignore[arg-type]
    listener.bot_id = "B1"
    return listener, slack, sent


def test_submit_publishes_the_whole_thread_and_round_trips(lcfg: ListenerConfig) -> None:
    history = [{"ts": "1", "user": "U2", "text": "login is broken"},
               {"ts": "2", "user": "U1", "text": f"<@{BOT}> please fix"}]
    listener, slack, sent = make(lcfg, history)
    listener.submit({"type": "app_mention", "channel": "C", "ts": "2", "thread_ts": "1", "user": "U1",
                     "text": f"<@{BOT}> please fix"})
    task = decode_task(encode(sent[0]))
    assert (task.event_id, task.thread_ts, task.asker, task.status_ts) == ("C:2", "1", "Asha", "S1")
    assert [(m.name, m.text) for m in task.thread] == [("Ravi", "login is broken"), ("Asha", "please fix")]
    assert task.event.text == "please fix"
    assert slack.posted[0]["text"] == "Queued…"


def test_a_tag_in_the_channel_brings_recent_channel_messages(lcfg: ListenerConfig) -> None:
    listener, slack, sent = make(lcfg, [{"ts": "5", "user": "U1", "text": f"<@{BOT}> fix what Ravi said"}])
    slack.channel = [{"ts": "3", "user": "U2", "text": "checkout 500s since deploy"},
                     {"ts": "4", "bot_id": "B1", "text": "my old reply"},
                     {"ts": "4.5", "user": "U1", "text": "same here"},
                     {"ts": "5", "user": "U1", "text": f"<@{BOT}> fix what Ravi said"}]
    listener.submit({"type": "app_mention", "channel": "C", "ts": "5", "user": "U1",
                     "text": f"<@{BOT}> fix what Ravi said"})
    task = decode_task(encode(sent[0]))
    assert [(m.name, m.text) for m in task.channel_recent] == [("Ravi", "checkout 500s since deploy"),
                                                               ("Asha", "same here")]


def test_a_tag_inside_a_thread_does_not_read_the_channel(lcfg: ListenerConfig) -> None:
    history = [{"ts": "1", "user": "U2", "text": "x"}, {"ts": "2", "user": "U1", "text": f"<@{BOT}> y"}]
    listener, slack, sent = make(lcfg, history)
    listener.submit({"type": "app_mention", "channel": "C", "ts": "2", "thread_ts": "1", "user": "U1",
                     "text": f"<@{BOT}> y"})
    assert slack.history_reads == 0 and sent[0].channel_recent == []


def test_duplicate_delivery_is_ignored(lcfg: ListenerConfig) -> None:
    listener, _, sent = make(lcfg, [{"ts": "1", "user": "U1", "text": "x"}])
    event = {"type": "app_mention", "channel": "C", "ts": "1", "user": "U1", "text": "x"}
    listener.submit(event)
    listener.submit(event)
    assert len(sent) == 1


def test_guest_is_refused_and_nothing_is_published(lcfg: ListenerConfig) -> None:
    listener, slack, sent = make(lcfg, [])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "1", "user": "G1", "text": "x"})
    assert sent == []
    assert slack.posted[0]["text"] == "Only members of this Slack workspace can use me."


def test_updates_go_only_to_known_status_messages(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [{"ts": "1", "user": "U1", "text": "x"}])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "1", "user": "U1", "text": "x"})
    listener.on_update(Update("progress", "S-unknown", status="Working… spoofed"))
    assert slack.updated == []
    listener.on_update(Update("progress", "S1", status="Working… Bash: make test"))
    listener.on_update(Update("result", "S1", status="Done in 3 min", text="**Fixed** it"))
    listener.on_update(Update("progress", "S1", status="Working… late"))
    assert [u["text"] for u in slack.updated] == ["Working… Bash: make test", "Done in 3 min"]
    assert slack.posted[-1] == {"channel": "C", "thread_ts": "1", "text": "*Fixed* it"}


def test_heartbeat_reaches_the_vm(lcfg: ListenerConfig) -> None:
    listener, _, _ = make(lcfg, [])
    listener.on_update(Update("heartbeat", busy=2))
    assert listener.vm.beats == [2]  # type: ignore[attr-defined]


def test_long_threads_are_paged_and_keep_the_request(lcfg: ListenerConfig) -> None:
    history = [{"ts": str(i), "user": "U2", "text": f"m{i}"} for i in range(1, 6)]
    listener, _, sent = make(lcfg, history)
    listener.submit({"type": "app_mention", "channel": "C", "ts": "5", "thread_ts": "1", "user": "U1", "text": "m5"})
    assert [m.text for m in sent[0].thread] == ["m1", "m2", "m3", "m4", "m5"]


def test_queue_failure_is_reported_and_not_left_pending(lcfg: ListenerConfig) -> None:
    listener, slack, sent = make(lcfg, [{"ts": "1", "user": "U1", "text": "boom"}])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "1", "user": "U1", "text": "boom"})
    assert sent == []
    assert slack.updated[-1]["text"] == "Could not queue this: ratelimited"
    assert listener.store.pending("S1") is None


def test_bots_own_messages_are_left_out_of_the_thread(lcfg: ListenerConfig) -> None:
    history = [{"ts": "1", "user": "U2", "text": "login is broken"},
               {"ts": "2", "user": "U1", "text": f"<@{BOT}> please fix"},
               {"ts": "3", "user": BOT, "bot_id": "B1", "text": "Queued…"},
               {"ts": "4", "user": "U1", "text": f"<@{BOT}> again"}]
    listener, _, sent = make(lcfg, history)
    listener.submit({"type": "app_mention", "channel": "C", "ts": "4", "thread_ts": "1", "user": "U1",
                     "text": f"<@{BOT}> again"})
    assert [m.text for m in sent[0].thread] == ["login is broken", "please fix", "again"]


def test_result_swaps_eyes_for_done_or_failed(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [])
    listener.store.add_pending("S1", "C", "1")
    listener.store.add_pending("S2", "C", "1")
    listener.on_update(Update("result", "S1", status="Done", text="ok", event_ts="2"))
    listener.on_update(Update("result", "S2", status="Failed", text="no", event_ts="3", failed=True))
    assert slack.reactions == [("remove", "2", "eyes"), ("add", "2", "white_check_mark"),
                               ("remove", "3", "eyes"), ("add", "3", "x")]


def test_progress_leaves_the_reaction_alone(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [])
    listener.store.add_pending("S1", "C", "1")
    listener.on_update(Update("progress", "S1", status="◐ Working · 5s"))
    assert slack.reactions == []


def test_root_message_is_kept_and_messages_with_our_bot_id_are_dropped(lcfg: ListenerConfig) -> None:
    history = [{"ts": "1", "user": BOT, "bot_id": "B1", "text": "Deploy report"},
               {"ts": "2", "user": "U1", "text": f"<@{BOT}> please fix"},
               {"ts": "3", "bot_id": "B1", "text": "Working…"},
               {"ts": "4", "bot_id": "B9", "user": "U2", "text": "other bot"}]
    listener, _, sent = make(lcfg, history)
    listener.submit({"type": "app_mention", "channel": "C", "ts": "2", "thread_ts": "1", "user": "U1",
                     "text": f"<@{BOT}> please fix"})
    assert [m.text for m in sent[0].thread] == ["Deploy report", "please fix", "other bot"]


def test_rate_limited_final_update_is_retried_before_pending_is_removed(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [])
    sleeps: list[float] = []
    listener.sleep = sleeps.append
    listener.store.add_pending("S1", "C", "1")
    slack.update_failures = [RateLimited("3"), RateLimited("7")]
    listener.on_update(Update("result", "S1", status="Done", text="the answer", event_ts="2"))
    assert sleeps == [3.0, 7.0]
    assert slack.updated[-1]["text"] == "Done" and slack.posted[-1]["text"] == "the answer"
    assert listener.store.pending("S1") is None


def test_persistently_rate_limited_result_stays_pending(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [])
    listener.sleep = lambda _: None
    listener.store.add_pending("S1", "C", "1")
    slack.update_failures = [RateLimited("1") for _ in range(5)]
    with pytest.raises(SlackApiError):
        listener.on_update(Update("result", "S1", status="Done", text="the answer", event_ts="2"))
    assert listener.store.pending("S1") == ("C", "1") and slack.posted == []


def test_could_not_queue_swaps_eyes_for_x(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [{"ts": "1", "user": "U1", "text": "boom"}])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "1", "user": "U1", "text": "boom"})
    assert slack.reactions[-2:] == [("remove", "1", "eyes"), ("add", "1", "x")]


def test_could_not_start_the_worker_swaps_eyes_for_x(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [])
    listener.vm.wake_error = RuntimeError("quota")  # type: ignore[attr-defined]
    listener._wake("C", "S1", "2")
    assert "Could not start the worker" in slack.updated[-1]["text"]
    assert slack.reactions == [("remove", "2", "eyes"), ("add", "2", "x")]


def test_status_carries_the_session_link(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [{"ts": "1", "user": "U1", "text": "x"}])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "1", "user": "U1", "text": "x"})
    listener.on_update(Update("progress", "S1", status="◐ Working · 5s", session_url="https://s/x.html"))
    assert slack.updated[-1]["text"] == "◐ Working · 5s · <https://s/x.html|View session>"


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("hold 30m", 30), ("hold 2h", 120), ("hold 45", 45), ("HOLD 1 hour", 60), ("hold 10h", 240),
     ("hold 0", 1), ("hold off", 0), ("release", 0), ("hold the door", None), ("please hold 30m", None),
     ("fix the login bug", None)],
)
def test_parse_hold(text: str, minutes: int | None) -> None:
    assert parse_hold(text) == minutes


def test_hold_command_holds_the_worker_without_queuing_a_task(lcfg: ListenerConfig) -> None:
    listener, slack, sent = make(lcfg, [])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "1", "user": "U1", "text": f"<@{BOT}> hold 30m"})
    assert sent == []
    assert listener.vm.holds == [1800]  # type: ignore[attr-defined]
    assert slack.posted[-1]["text"].startswith("Holding the worker awake until ")


def test_hold_off_releases(lcfg: ListenerConfig) -> None:
    listener, slack, sent = make(lcfg, [])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "2", "user": "U1", "text": f"<@{BOT}> hold off"})
    assert sent == [] and listener.vm.holds == [0]  # type: ignore[attr-defined]
    assert slack.posted[-1]["text"].startswith("Hold released")


def test_guests_cannot_hold(lcfg: ListenerConfig) -> None:
    listener, slack, _ = make(lcfg, [])
    listener.submit({"type": "app_mention", "channel": "C", "ts": "3", "user": "G1", "text": f"<@{BOT}> hold 2h"})
    assert listener.vm.holds == []  # type: ignore[attr-defined]
    assert slack.posted[-1]["text"] == "Only members of this Slack workspace can use me."
