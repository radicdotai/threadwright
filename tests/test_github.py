import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from threadwright import github, workspace

BRANCH = "threadwright/" + workspace.thread_slug("C0ABC", "1795672308.099549")


def run(conclusion: str = "failure", author: str = "threadwright[bot]", branch: str = BRANCH) -> dict[str, object]:
    return {"action": "completed", "repository": {"full_name": "acme/api"},
            "workflow_run": {"conclusion": conclusion, "head_branch": branch, "head_sha": "abc1234def", "name": "ci",
                             "html_url": "https://gh/run/1", "pull_requests": [{"number": 7}],
                             "head_commit": {"author": {"name": author, "email": "x@y"}}}}


def review(login: str = "ravi", state: str = "changes_requested", kind: str = "User") -> dict[str, object]:
    return {"action": "submitted", "repository": {"full_name": "acme/api"},
            "review": {"state": state, "body": "rename this", "html_url": "https://gh/r/1",
                       "user": {"login": login, "type": kind}},
            "pull_request": {"number": 7, "html_url": "https://gh/pr/7", "head": {"ref": BRANCH, "sha": "abc"}}}


def test_branch_maps_back_to_its_slack_thread() -> None:
    assert github.thread_of(BRANCH) == ("C0ABC", "1795672308.099549")
    assert github.thread_of("feature/x") is None


def test_only_failed_runs_of_the_bots_own_commits_on_its_branches_count() -> None:
    event = github.pr_event("workflow_run", run(), frozenset())
    assert event and (event.kind, event.pr, event.repo) == ("ci", 7, "acme/api")
    assert github.pr_event("workflow_run", run(conclusion="success"), frozenset()) is None
    assert github.pr_event("workflow_run", run(author="Ravi"), frozenset()) is None
    assert github.pr_event("workflow_run", run(author="Radic Bot"), frozenset({"Radic Bot"}))
    assert github.pr_event("workflow_run", run(branch="main"), frozenset()) is None


def test_reviews_from_people_count_but_approvals_and_bots_do_not() -> None:
    event = github.pr_event("pull_request_review", review(), frozenset())
    assert event and event.kind == "review" and event.by == "ravi" and event.detail == "rename this"
    assert github.pr_event("pull_request_review", review(state="approved"), frozenset()) is None
    assert github.pr_event("pull_request_review", review(kind="Bot"), frozenset()) is None
    assert github.pr_event("pull_request_review", review(login="ci-bot"), frozenset({"ci-bot"})) is None


def test_signature_check_needs_the_secret() -> None:
    body = b'{"a":1}'
    import hashlib
    import hmac
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert github.signed("s3cret", body, good)
    assert not github.signed("s3cret", body + b" ", good)
    assert not github.signed("", body, good)


def test_event_round_trips_through_pubsub() -> None:
    event = github.pr_event("pull_request_review", review(), frozenset())
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
