import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from threadwright import github, workspace

BRANCH = "threadwright/" + workspace.thread_slug("C0ABC", "1795672308.099549")


BOTS = frozenset({"tw-app[bot]"})


def run(conclusion: str = "failure", actor: str = "tw-app[bot]", branch: str = BRANCH,
        head: str = "acme/api") -> dict[str, object]:
    return {"action": "completed", "repository": {"full_name": "acme/api"},
            "workflow_run": {"conclusion": conclusion, "head_branch": branch, "head_sha": "abc1234def", "name": "ci",
                             "html_url": "https://gh/run/1", "pull_requests": [{"number": 7}],
                             "actor": {"login": actor}, "head_repository": {"full_name": head},
                             "head_commit": {"author": {"name": "x[bot]", "email": "x@y"}}}}


def review(login: str = "ravi", state: str = "changes_requested", kind: str = "User", role: str = "MEMBER",
           head: str = "acme/api") -> dict[str, object]:
    return {"action": "submitted", "repository": {"full_name": "acme/api"},
            "review": {"state": state, "body": "rename this", "html_url": "https://gh/r/1",
                       "author_association": role, "user": {"login": login, "type": kind}},
            "pull_request": {"number": 7, "html_url": "https://gh/pr/7",
                             "head": {"ref": BRANCH, "sha": "abc", "repo": {"full_name": head}}}}


def test_branch_maps_back_to_its_slack_thread() -> None:
    assert github.thread_of(BRANCH) == ("C0ABC", "1795672308.099549")
    assert github.thread_of("feature/x") is None


def test_only_failed_runs_pushed_by_the_bot_on_its_own_branches_count() -> None:
    event = github.pr_event("workflow_run", run(), BOTS)
    assert event and (event.kind, event.pr, event.repo) == ("ci", 7, "acme/api")
    assert github.pr_event("workflow_run", run(conclusion="success"), BOTS) is None
    assert github.pr_event("workflow_run", run(actor="ravi"), BOTS) is None
    assert github.pr_event("workflow_run", run(actor="human[bot]"), BOTS) is None
    assert github.pr_event("workflow_run", run(head="evil/api"), BOTS) is None
    assert github.pr_event("workflow_run", run(branch="main"), BOTS) is None


def test_only_reviews_from_trusted_people_on_same_repo_branches_count() -> None:
    event = github.pr_event("pull_request_review", review(), BOTS)
    assert event and event.kind == "review" and event.by == "ravi" and event.detail == "rename this"
    assert github.pr_event("pull_request_review", review(state="approved"), BOTS) is None
    assert github.pr_event("pull_request_review", review(kind="Bot"), BOTS) is None
    assert github.pr_event("pull_request_review", review(login="tw-app[bot]"), BOTS) is None
    assert github.pr_event("pull_request_review", review(role="NONE"), BOTS) is None
    assert github.pr_event("pull_request_review", review(role="CONTRIBUTOR"), BOTS) is None
    assert github.pr_event("pull_request_review", review(head="stranger/api"), BOTS) is None


def test_signature_check_needs_the_secret() -> None:
    body = b'{"a":1}'
    import hashlib
    import hmac
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert github.signed("s3cret", body, good)
    assert not github.signed("s3cret", body + b" ", good)
    assert not github.signed("", body, good)
    assert not github.signed("s3cret", body, "sha256=\u00e9")


def test_event_round_trips_through_pubsub() -> None:
    event = github.pr_event("pull_request_review", review(), BOTS)
    assert event and github.from_json(github.to_json(event)) == event


def pad(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def test_app_token_is_signed_and_cached() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    calls: list[tuple[str, str]] = []

    def post(url: str, jwt: str) -> dict[str, object]:
        calls.append((url, jwt))
        return {"token": "ghs_x"}

    tokens = github.AppTokens("123", "456", pem, post)
    assert tokens.token() == tokens.token() == "ghs_x"
    assert len(calls) == 1 and calls[0][0].endswith("/app/installations/456/access_tokens")
    head, body, sig = calls[0][1].split(".")
    assert json.loads(pad(body))["iss"] == "123"
    key.public_key().verify(pad(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
