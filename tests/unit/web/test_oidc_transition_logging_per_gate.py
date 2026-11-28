"""Every gate that accepts the migration list logs one INFO line for a transition-only identity, none otherwise."""
import logging
from unittest.mock import patch

from src.web.indexer_worker_app import worker_oidc_gate
from src.web.worker_oidc_verifier import build_oidc_gate, transition_only_emails

_SA = "alek-bot@my-project.iam.gserviceaccount.com"
_RELAY = "relay@my-project.iam.gserviceaccount.com"
_OLD = "old-sa@my-project.iam.gserviceaccount.com"
_VERIFY = "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token"


def _lines(caplog):
    return [r for r in caplog.records if "transition list" in r.getMessage()]


async def _call(gate, email, caplog):
    caplog.set_level(logging.INFO)
    with patch(_VERIFY, return_value={"email": email, "email_verified": True}):
        return await gate("Bearer good.jwt.token")


def test_the_transition_set_leaves_out_a_gates_own_identities():
    assert transition_only_emails(f"{_OLD}, {_RELAY}", _SA, _RELAY) == {_OLD}
    assert transition_only_emails(None, _SA) == frozenset()


async def test_the_voice_gate_logs_a_transition_only_identity_once(caplog):
    gate = build_oidc_gate({_SA, _RELAY, _OLD}, transition_emails=transition_only_emails(_OLD, _SA, _RELAY))
    assert await _call(gate, _OLD, caplog) is True
    assert len(_lines(caplog)) == 1 and _lines(caplog)[0].levelname == "INFO"


async def test_the_voice_gate_logs_nothing_for_its_own_identities(caplog):
    gate = build_oidc_gate({_SA, _RELAY, _OLD}, transition_emails=transition_only_emails(_OLD, _SA, _RELAY))
    assert await _call(gate, _RELAY, caplog) is True
    assert _lines(caplog) == []


async def test_the_indexer_gate_logs_a_transition_only_identity_once(caplog):
    gate = worker_oidc_gate({_SA, _OLD}, transition_emails=transition_only_emails(_OLD, _SA))
    assert await _call(gate, _OLD, caplog) is True
    assert len(_lines(caplog)) == 1 and _lines(caplog)[0].levelname == "INFO"


async def test_the_indexer_gate_logs_nothing_for_its_own_identity(caplog):
    gate = worker_oidc_gate({_SA, _OLD}, transition_emails=transition_only_emails(_OLD, _SA))
    assert await _call(gate, _SA, caplog) is True
    assert _lines(caplog) == []
