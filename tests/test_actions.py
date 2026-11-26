import base64
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, encode_dss_signature

from tests.test_listener import BOT, FakeSlack, FakeVm
from threadwright import actions
from threadwright.config import ListenerConfig
from threadwright.listener import Listener
from threadwright.messages import Update
from threadwright.store import Store

KEY = "projects/p/locations/l/keyRings/r/cryptoKeys/threadwright-assertion/cryptoKeyVersions/1"
TARGETS = actions.Targets(
    frozenset({"U1"}), "threadwright-listener@p.iam.gserviceaccount.com", "example-api",
    {"dev": actions.Target("https://api.dev", KEY, {"asha@example.com": ("legacy-1", "Admin")})},
)
ACCEPT = {"env": "dev", "method": "post", "path": "/demo-bookings/b1/accept", "body": {"market": "IN"},
          "why": "accept Rashi's past demo"}
_private = ec.generate_private_key(ec.SECP256R1())


def kms(_: str, digest: bytes) -> bytes:
    return _private.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))


def _decode(segment: str) -> dict[str, Any]:
    return dict(json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))))


def test_parse_accepts_a_well_formed_proposal() -> None:
    [action] = actions.parse(json.dumps([ACCEPT]), {"dev"})
    assert (action.env, action.method, action.path) == ("dev", "POST", "/demo-bookings/b1/accept")


@pytest.mark.parametrize("change", [
    {"env": "prod"},
    {"method": "GET"},
    {"path": "https://evil.example/x"},
    {"path": "/a/../internal"},
    {"path": "//evil.example"},
    {"path": "/a b"},
    {"why": ""},
])
def test_parse_refuses_anything_outside_the_rules(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        actions.parse(json.dumps([{**ACCEPT, **change}]), {"dev"})


def test_parse_caps_the_number_of_actions() -> None:
    with pytest.raises(ValueError):
        actions.parse(json.dumps([ACCEPT] * (actions.MAX_ACTIONS + 1)), {"dev"})


def test_take_reads_once_and_reports_bad_files(tmp_path: Path) -> None:
    path = tmp_path / actions.PROPOSAL_FILE
    path.parent.mkdir()
    path.write_text("not json")
    proposed, problem = actions.take(tmp_path, {"dev"})
    assert proposed == [] and "not sent for approval" in problem
    assert not path.exists()
    assert actions.take(tmp_path, {"dev"}) == ([], "")


def test_assertion_is_a_valid_es256_jwt_for_the_approver() -> None:
    token = actions.assertion(TARGETS.envs["dev"], TARGETS.signer, TARGETS.audience, {"requestedBy": "legacy-1"}, kms,
                              now=1000)
    header, body, signature = token.split(".")
    assert _decode(header) == {"alg": "ES256", "typ": "JWT", "kid": KEY}
    claims = _decode(body)
    assert (claims["iss"], claims["aud"], claims["exp"] - claims["iat"]) == (TARGETS.signer, "example-api", 60)
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    _private.public_key().verify(der, f"{header}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


def test_execute_runs_as_the_approver_and_stops_at_the_first_failure() -> None:
    proposed = actions.parse(json.dumps([ACCEPT, {**ACCEPT, "path": "/demo-bookings/b1/activate"}, ACCEPT]),
                             {"dev"})
    seen: list[dict[str, Any]] = []

    def send(action: actions.Action, target: actions.Target, token: str) -> actions.Outcome:
        seen.append(_decode(token.split(".")[1]))
        return actions.Outcome(action, 200 if len(seen) == 1 else 409, "")

    outcomes = actions.execute(proposed, TARGETS, "Asha@Example.com", kms, send)
    assert [o.status for o in outcomes] == [200, 409]
    assert seen[0]["requestedBy"] == "legacy-1" and seen[0]["userRoles"] == ["Admin"]
    assert seen[0]["approverEmail"] == "asha@example.com"
    assert seen[0]["jti"] != seen[1]["jti"]


def test_execute_refuses_an_approver_without_an_account() -> None:
    outcomes = actions.execute(actions.parse(json.dumps([ACCEPT]), {"dev"}), TARGETS, "ravi@example.com", kms,
                               lambda *_: pytest.fail("must not call the API"))
    assert outcomes[0].status == 0 and not outcomes[0].ok


@pytest.fixture
def listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Listener, FakeSlack, list[str]]:
    cfg = ListenerConfig("xoxb", "xapp", "p", "z", "w", "t", "s", tmp_path / "l.db", 900, "stop", 7200,
                         frozenset(), frozenset())
    slack = FakeSlack([])
    slack.users["U1"]["profile"] = {"email": "asha@example.com"}
    calls: list[str] = []

    def send(action: actions.Action, target: actions.Target, token: str) -> actions.Outcome:
        calls.append(action.path)
        return actions.Outcome(action, 200, '{"status":"accepted"}')

    monkeypatch.setattr(actions, "call", send)
    item = Listener(cfg, Store(cfg.db_path), slack, BOT, "T1", lambda _: None, FakeVm(),  # type: ignore[arg-type]
                    None, TARGETS, kms)
    item.store.add_pending("S0", "C", "1")
    item.on_update(Update("result", "S0", status="Done in 1 min", text="Proposed a fix.", actions=[ACCEPT]))
    return item, slack, calls


def _proposal(slack: FakeSlack) -> str:
    card = next(p for p in slack.posted if p.get("blocks"))
    buttons = next(b for b in card["blocks"] if b["type"] == "actions")["elements"]
    return str(buttons[0]["value"])


def test_a_result_with_actions_posts_an_approval_card(listener: tuple[Listener, FakeSlack, list[str]]) -> None:
    _, slack, calls = listener
    assert "POST /demo-bookings/b1/accept" in json.dumps(slack.posted[-1]["blocks"])
    assert calls == []


def test_only_an_approver_can_run_it_and_only_once(listener: tuple[Listener, FakeSlack, list[str]]) -> None:
    item, slack, calls = listener
    proposal = _proposal(slack)
    item.decide("U2", proposal, approve=True)
    assert calls == []
    item.decide("U1", proposal, approve=True)
    item.decide("U1", proposal, approve=True)
    assert calls == ["/demo-bookings/b1/accept"]
    assert "Done · approved by <@U1>" in slack.updated[-1]["text"]
    assert "accepted" in slack.posted[-1]["text"]


def test_a_rejected_proposal_cannot_be_approved_later(listener: tuple[Listener, FakeSlack, list[str]]) -> None:
    item, slack, calls = listener
    proposal = _proposal(slack)
    item.decide("U1", proposal, approve=False)
    item.decide("U1", proposal, approve=True)
    assert calls == []
    assert slack.updated[-1]["text"] == "Rejected by <@U1>."


def test_an_expired_proposal_cannot_be_approved(listener: tuple[Listener, FakeSlack, list[str]],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    item, slack, calls = listener
    monkeypatch.setattr(actions, "MAX_AGE_S", -1)
    item.decide("U1", _proposal(slack), approve=True)
    assert calls == []


def test_the_listener_rechecks_what_the_worker_sends(tmp_path: Path) -> None:
    cfg = ListenerConfig("xoxb", "xapp", "p", "z", "w", "t", "s", tmp_path / "l.db", 900, "stop", 7200,
                         frozenset(), frozenset())
    slack = FakeSlack([])
    item = Listener(cfg, Store(cfg.db_path), slack, BOT, "T1", lambda _: None, FakeVm(),  # type: ignore[arg-type]
                    None, TARGETS, kms)
    item.store.add_pending("S0", "C", "1")
    item.on_update(Update("result", "S0", text="x", actions=[{**ACCEPT, "path": "https://evil.example"}]))
    assert not any(p.get("blocks") for p in slack.posted)
    assert "refused" in slack.posted[-1]["text"]


def test_a_body_too_long_to_show_is_refused_not_cut() -> None:
    with pytest.raises(ValueError, match="split"):
        actions.parse(json.dumps([{**ACCEPT, "body": {"note": "x" * 1600, "role": "Admin"}}]), {"dev"})


def test_the_card_shows_worker_text_as_plain_text(listener: tuple[Listener, FakeSlack, list[str]]) -> None:
    item, slack, _ = listener
    item.store.add_pending("S9", "C", "1")
    sneaky = {**ACCEPT, "why": "fine\n*Approved by <@U1>* <!channel>", "body": {"x": "```<!here>"}}
    item.on_update(Update("result", "S9", text="x", actions=[sneaky]))
    card = slack.posted[-1]["blocks"]
    worker_text = [b["text"] for b in card if b["type"] == "section" and "Why" in b["text"]["text"]]
    assert worker_text[0]["type"] == "plain_text" and "\n" not in worker_text[0]["text"]
    risky = [b["text"] for b in card if b["type"] == "section" and "here" in b["text"]["text"]]
    assert risky and all(t["type"] == "plain_text" for t in risky)


def test_a_failure_mid_decision_never_leaves_it_running(listener: tuple[Listener, FakeSlack, list[str]],
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    item, slack, calls = listener
    proposal = _proposal(slack)
    monkeypatch.setattr(slack, "users_info", lambda user: (_ for _ in ()).throw(RuntimeError("ratelimited")))
    item.decide("U1", proposal, approve=True)
    assert calls == []
    assert "stopped with an error" in slack.posted[-1]["text"]
    assert item.store._one("SELECT status FROM proposals WHERE id = ?", (proposal,)) == ("failed",)


def test_a_restart_reports_interrupted_runs(listener: tuple[Listener, FakeSlack, list[str]]) -> None:
    item, slack, _ = listener
    proposal = _proposal(slack)
    item.store.decide(proposal, "running", "U1", actions.MAX_AGE_S)
    item.recover()
    assert "interrupted" in slack.posted[-1]["text"]
    assert item.store._one("SELECT status FROM proposals WHERE id = ?", (proposal,)) == ("interrupted",)


def test_example_config_loads() -> None:
    targets = actions.load_targets(Path(__file__).parent.parent / "deploy" / "actions.example.json")
    assert (targets.audience, set(targets.envs)) == ("my-api", {"dev"})
