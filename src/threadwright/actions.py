import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from threadwright.session import redact

PROPOSAL_FILE = ".threadwright/actions.json"
MAX_ACTIONS = 5
MAX_AGE_S = 24 * 3600
_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PATH = re.compile(r"^/[A-Za-z0-9._~\-/]*(\?[A-Za-z0-9._~\-=&%]*)?$")
_ASSERTION_S = 60
_RESULT_CHARS = 1500
MAX_BODY_CHARS = 1500
_MAX_PATH_CHARS = 300


@dataclass(frozen=True)
class Action:
    env: str
    method: str
    path: str
    body: Any
    why: str


@dataclass(frozen=True)
class Target:
    api: str
    key_version: str
    users: dict[str, tuple[str, str]]


@dataclass(frozen=True)
class Targets:
    approvers: frozenset[str]
    signer: str
    audience: str
    envs: dict[str, Target]


def load_targets(path: Path) -> Targets:
    raw = json.loads(path.read_text())
    envs = {
        name: Target(env["api"].rstrip("/"), env["key_version"],
                     {email.lower(): (user[0], user[1]) for email, user in env["users"].items()})
        for name, env in raw["envs"].items()
    }
    return Targets(frozenset(raw["approvers"]), raw["signer"], raw["audience"], envs)


def parse(text: str, envs: set[str]) -> list[Action]:
    raw = json.loads(text)
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ACTIONS:
        raise ValueError(f"expected a list of 1 to {MAX_ACTIONS} actions")
    actions = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each action must be an object")
        env, method, path = item.get("env"), str(item.get("method", "")).upper(), item.get("path")
        if env not in envs:
            raise ValueError(f"unknown env {env!r}; use one of {sorted(envs)}")
        if method not in _METHODS:
            raise ValueError(f"method must be one of {sorted(_METHODS)}")
        if not isinstance(path, str) or len(path) > _MAX_PATH_CHARS or not _PATH.match(path) \
                or ".." in path or "//" in path:
            raise ValueError(f"bad path {path!r}")
        if len(show_body(item.get("body"))) > MAX_BODY_CHARS:
            raise ValueError(f"body is over {MAX_BODY_CHARS} characters; split the change")
        why = " ".join(str(item.get("why") or "").split())
        if not why:
            raise ValueError("each action needs a 'why'")
        actions.append(Action(env, method, path, item.get("body"), why[:300]))
    return actions


def show_body(body: Any) -> str:
    return "" if body is None else json.dumps(body, indent=1)


def take(root: Path, envs: set[str]) -> tuple[list[Action], str]:
    path = root / PROPOSAL_FILE
    if not path.exists():
        return [], ""
    try:
        return parse(path.read_text(), envs), ""
    except (ValueError, json.JSONDecodeError) as exc:
        return [], f"Proposed actions were not sent for approval: {exc}"
    finally:
        path.unlink(missing_ok=True)


def to_dicts(actions: list[Action]) -> list[dict[str, Any]]:
    return [asdict(a) for a in actions]


def from_dicts(raw: list[dict[str, Any]]) -> list[Action]:
    return [Action(**a) for a in raw]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def der_to_raw(der: bytes) -> bytes:
    if der[0] != 0x30:
        raise ValueError("not a DER sequence")
    i = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    parts = []
    for _ in range(2):
        if der[i] != 0x02:
            raise ValueError("not a DER integer")
        n = der[i + 1]
        parts.append(int.from_bytes(der[i + 2:i + 2 + n], "big").to_bytes(32, "big"))
        i += 2 + n
    return parts[0] + parts[1]


def assertion(target: Target, signer: str, audience: str, claims: dict[str, Any],
              sign: Callable[[str, bytes], bytes], now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    header = {"alg": "ES256", "typ": "JWT", "kid": target.key_version}
    body = {"iss": signer, "aud": audience, "iat": issued, "exp": issued + _ASSERTION_S,
            "jti": str(uuid.uuid4()), **claims}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(body).encode())}"
    signature = der_to_raw(sign(target.key_version, hashlib.sha256(signing_input.encode()).digest()))
    return f"{signing_input}.{_b64url(signature)}"


def kms_signer() -> Callable[[str, bytes], bytes]:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]

    def sign(key_version: str, digest: bytes) -> bytes:
        response = session.post(f"https://cloudkms.googleapis.com/v1/{key_version}:asymmetricSign",
                                json={"digest": {"sha256": base64.b64encode(digest).decode()}}, timeout=15)
        response.raise_for_status()
        return base64.b64decode(response.json()["signature"])

    return sign


@dataclass(frozen=True)
class Outcome:
    action: Action
    status: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def call(action: Action, target: Target, token: str, timeout: float = 60) -> Outcome:
    data = None if action.body is None else json.dumps(action.body).encode()
    request = urllib.request.Request(f"{target.api}{action.path}", data=data, method=action.method,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except OSError as exc:
        return Outcome(action, 0, f"request failed: {exc}")
    return Outcome(action, status, redact(raw.decode(errors="replace"))[:_RESULT_CHARS])


def execute(actions: list[Action], targets: Targets, approver_email: str,
            sign: Callable[[str, bytes], bytes],
            send: Callable[[Action, Target, str], Outcome] | None = None) -> list[Outcome]:
    send = send or call
    outcomes: list[Outcome] = []
    for action in actions:
        target = targets.envs[action.env]
        user = target.users.get(approver_email.lower())
        if user is None:
            outcomes.append(Outcome(action, 0, f"{approver_email} has no account mapped for {action.env}"))
            break
        user_id, role = user
        claims = {"sub": user_id, "requestedBy": user_id, "userRoles": [role], "approverEmail": approver_email.lower()}
        token = assertion(target, targets.signer, targets.audience, claims, sign)
        outcome = send(action, target, token)
        outcomes.append(outcome)
        if not outcome.ok:
            break
    return outcomes
