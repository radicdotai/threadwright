import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class SlackFile:
    name: str
    data: str


@dataclass(frozen=True)
class SlackMessage:
    name: str
    text: str
    files: list[SlackFile] = field(default_factory=list)


@dataclass(frozen=True)
class Task:
    event_id: str
    channel: str
    thread_ts: str
    status_ts: str
    asker: str
    event: SlackMessage
    thread: list[SlackMessage]
    channel_recent: list[SlackMessage] = field(default_factory=list)
    kind: str = "task"
    plan_only: bool = False
    fresh: bool = False


@dataclass(frozen=True)
class Update:
    kind: str
    status_ts: str = ""
    status: str = ""
    text: str = ""
    busy: int = 0
    event_ts: str = ""
    failed: bool = False
    session_url: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    plan: str = ""
    question: dict[str, Any] = field(default_factory=dict)
    channel: str = ""
    thread_ts: str = ""


def encode(item: Task | Update) -> bytes:
    return json.dumps(asdict(item)).encode()


def _message(raw: dict[str, Any]) -> SlackMessage:
    return SlackMessage(raw["name"], raw["text"], [SlackFile(**f) for f in raw.get("files", [])])


def decode_task(data: bytes) -> Task:
    raw = json.loads(data)
    return Task(raw["event_id"], raw["channel"], raw["thread_ts"], raw["status_ts"], raw["asker"],
                _message(raw["event"]), [_message(m) for m in raw["thread"]],
                [_message(m) for m in raw.get("channel_recent", [])], raw.get("kind", "task"),
                bool(raw.get("plan_only")), bool(raw.get("fresh")))


def decode_update(data: bytes) -> Update:
    raw = json.loads(data)
    known = {f.name for f in fields(Update)}
    return Update(**{k: v for k, v in raw.items() if k in known})
