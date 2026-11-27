import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger(__name__)

BRANCH_PREFIX = "threadwright/"
_SLUG = re.compile(r"^([A-Z0-9]+)-(\d+)-(\d+)$")
_TOKEN_TTL_S = 5 * 60
_MAX_BODY = 4000
_MAX_PAYLOAD = 1024 * 1024
_TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass(frozen=True)
class PrEvent:
    kind: str
    repo: str
    branch: str
    pr: int
    url: str
    sha: str
    by: str
    detail: str
    link: str


def thread_of(branch: str) -> tuple[str, str] | None:
    match = _SLUG.match(branch.removeprefix(BRANCH_PREFIX)) if branch.startswith(BRANCH_PREFIX) else None
    return (match.group(1), f"{match.group(2)}.{match.group(3)}") if match else None


def signed(secret: str, body: bytes, signature: str) -> bool:
    expected = ("sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()).encode()
    return bool(secret) and hmac.compare_digest(expected, signature.encode("utf-8", "replace"))


def pr_event(kind: str, payload: dict[str, Any], bots: frozenset[str]) -> PrEvent | None:
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    if kind == "workflow_run" and payload.get("action") == "completed":
        run = payload.get("workflow_run") or {}
        actor = str((run.get("triggering_actor") or run.get("actor") or {}).get("login") or "")
        prs = run.get("pull_requests") or []
        if run.get("conclusion") != "failure" or not prs or not thread_of(str(run.get("head_branch") or "")):
            return None
        if actor not in bots or (run.get("head_repository") or {}).get("full_name") != repo:
            return None
        number = int(prs[0]["number"])
        return PrEvent("ci", repo, str(run["head_branch"]), number, f"https://github.com/{repo}/pull/{number}",
                       str(run.get("head_sha") or ""), "", str(run.get("name") or "CI"), str(run.get("html_url") or ""))
    if kind == "pull_request_review" and payload.get("action") == "submitted":
        review, pr = payload.get("review") or {}, payload.get("pull_request") or {}
        user = review.get("user") or {}
        branch = str((pr.get("head") or {}).get("ref") or "")
        if review.get("state") not in ("commented", "changes_requested") or not thread_of(branch):
            return None
        if user.get("type") == "Bot" or str(user.get("login") or "") in bots:
            return None
        if review.get("author_association") not in _TRUSTED:
            return None
        if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo:
            return None
        return PrEvent("review", repo, branch, int(pr["number"]), str(pr.get("html_url") or ""),
                       str((pr.get("head") or {}).get("sha") or ""), str(user.get("login") or ""),
                       str(review.get("body") or "")[:_MAX_BODY], str(review.get("html_url") or ""))
    return None


def to_json(event: PrEvent) -> bytes:
    return json.dumps(asdict(event)).encode()


def from_json(data: bytes) -> PrEvent:
    return PrEvent(**json.loads(data))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key_pem: bytes, now: float | None = None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    issued = int(time.time() if now is None else now)
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps({"iat": issued - 60, "exp": issued + 540, "iss": app_id}).encode())
    key = serialization.load_pem_private_key(key_pem, password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64url(signature)}"


def _post(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="POST", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return dict(json.loads(response.read()))


class AppTokens:
    def __init__(self, app_id: str, installation_id: str, key_pem: bytes,
                 post: Callable[[str, str], dict[str, Any]] = _post) -> None:
        self._app_id, self._installation, self._key = app_id, installation_id, key_pem
        self._post = post
        self._lock = threading.Lock()
        self._token, self._until = "", 0.0

    def token(self) -> str:
        with self._lock:
            if time.time() >= self._until:
                url = f"https://api.github.com/app/installations/{self._installation}/access_tokens"
                self._token = str(self._post(url, app_jwt(self._app_id, self._key))["token"])
                self._until = time.time() + _TOKEN_TTL_S
            return self._token


def serve(secret: str, bots: frozenset[str], publish: Callable[[bytes], None], port: int) -> None:
    class Hook(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def _reply(self, code: int) -> None:
            self.send_response(code)
            self.end_headers()

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return self._reply(400)
            if not 0 < length <= _MAX_PAYLOAD:
                return self._reply(413 if length else 400)
            body = self.rfile.read(length)
            if not signed(secret, body, self.headers.get("X-Hub-Signature-256", "")):
                return self._reply(401)
            try:
                event = pr_event(self.headers.get("X-GitHub-Event", ""), json.loads(body), bots)
                if event:
                    publish(to_json(event))
                    log.info("forwarded %s on %s#%s", event.kind, event.repo, event.pr)
            except Exception:
                log.exception("could not handle webhook")
                return self._reply(500)
            self._reply(204)

        def log_message(self, *_: Any) -> None:
            return

    ThreadingHTTPServer(("", port), Hook).serve_forever()


def run_receiver() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    clients: list[Any] = []

    def publish(data: bytes) -> None:
        import google.cloud.pubsub_v1 as pubsub_v1

        if not clients:
            clients.append(pubsub_v1.PublisherClient())
        topic = clients[0].topic_path(os.environ["GCP_PROJECT"], os.environ.get("GITHUB_TOPIC", "threadwright-github"))
        clients[0].publish(topic, data).result(timeout=30)

    bots = frozenset(p.strip() for p in os.environ.get("BOT_AUTHORS", "").split(",") if p.strip())
    serve(os.environ["GITHUB_WEBHOOK_SECRET"], bots, publish, int(os.environ.get("PORT", "8080")))
