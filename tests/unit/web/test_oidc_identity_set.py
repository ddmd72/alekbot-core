"""IAM least-privilege Step 0 (C1): the OIDC verifier accepts a SET of identities.

Mock boundary: google.oauth2.id_token.verify_oauth2_token. "Harmless until configured": with only the
single legacy identity (no new env/secret) the gates accept exactly what they accepted before.
"""
from unittest.mock import patch

import pytest

from src.web.indexer_worker_app import worker_oidc_gate
from src.web.worker_oidc_verifier import build_oidc_gate, parse_sa_emails, verify_worker_oidc

_OLD = "legacy@example.invalid"
_NEW = "invoker@example.invalid"
_RELAY = "relay@example.invalid"
_VERIFY = "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token"


def _claims(email, **extra):
    return {"email": email, "email_verified": True, **extra}


class TestParseSaEmails:
    def test_strips_whitespace_and_newlines(self):
        assert parse_sa_emails(f"  {_OLD}\n") == {_OLD}

    def test_splits_commas_and_unions_values(self):
        assert parse_sa_emails(f"{_OLD}, {_NEW}\n", _RELAY) == {_OLD, _NEW, _RELAY}

    def test_drops_empty_items_and_sentinel(self):
        assert parse_sa_emails(f",{_OLD},,-, ", "-") == {_OLD}

    @pytest.mark.parametrize("value", [None, "", "  ", "-", " - \n"])
    def test_nothing_configured_is_empty(self, value):
        assert parse_sa_emails(value) == frozenset()
        assert parse_sa_emails(None, value) == frozenset()

    def test_no_arguments(self):
        assert parse_sa_emails() == frozenset()


class TestVerifyWithSet:
    def test_member_accepted(self):
        with patch(_VERIFY, return_value=_claims(_NEW)):
            assert verify_worker_oidc("Bearer t", {_OLD, _NEW}) is True

    def test_non_member_rejected(self):
        with patch(_VERIFY, return_value=_claims("evil@example.invalid")):
            assert verify_worker_oidc("Bearer t", {_OLD, _NEW}) is False

    def test_empty_set_accepts_nothing_without_touching_the_token(self):
        with patch(_VERIFY, return_value=_claims(_OLD)) as verify:
            assert verify_worker_oidc("Bearer t", frozenset()) is False
        verify.assert_not_called()

    def test_str_is_a_type_error(self):
        with pytest.raises(TypeError):
            verify_worker_oidc("Bearer t", _OLD)  # type: ignore[arg-type]

    def test_claim_with_trailing_newline_rejected(self):
        with patch(_VERIFY, return_value=_claims(_OLD + "\n")):
            assert verify_worker_oidc("Bearer t", {_OLD}) is False

    def test_unverified_email_rejected(self):
        with patch(_VERIFY, return_value=_claims(_OLD, email_verified=False)):
            assert verify_worker_oidc("Bearer t", {_OLD}) is False

    def test_audience_still_enforced(self):
        with patch(_VERIFY, return_value=_claims(_OLD, aud="https://other")):
            assert verify_worker_oidc("Bearer t", {_OLD}, ["https://svc"]) is False

    def test_rejected_email_is_logged(self, caplog):
        with patch(_VERIFY, return_value=_claims("evil@example.invalid")):
            with caplog.at_level("WARNING"):
                verify_worker_oidc("Bearer t", {_OLD})
        assert "evil@example.invalid" in caplog.text


class TestBuildOidcGate:
    """What main.py wires for /worker and /voice/*."""

    async def test_legacy_only_config_accepts_exactly_what_it_did(self):
        gate = build_oidc_gate(parse_sa_emails(_OLD, None), on_cloud_run=True)
        with patch(_VERIFY, return_value=_claims(_OLD)):
            assert await gate("Bearer t") is True
        with patch(_VERIFY, return_value=_claims(_NEW)):
            assert await gate("Bearer t") is False

    async def test_transition_list_adds_identities(self):
        gate = build_oidc_gate(parse_sa_emails(_OLD, f"{_NEW},-"), on_cloud_run=True)
        with patch(_VERIFY, return_value=_claims(_NEW)):
            assert await gate("Bearer t") is True

    async def test_voice_gate_accepts_relay_identity_and_rejects_unknown(self):
        gate = build_oidc_gate(parse_sa_emails(_OLD, _RELAY, None), on_cloud_run=True)
        with patch(_VERIFY, return_value=_claims(_RELAY)):
            assert await gate("Bearer t") is True
        with patch(_VERIFY, return_value=_claims("evil@example.invalid")):
            assert await gate("Bearer t") is False

    async def test_empty_on_cloud_run_fails_closed_and_logs_error_once(self, caplog):
        with caplog.at_level("ERROR"):
            gate = build_oidc_gate(frozenset(), on_cloud_run=True, label="Worker")
        assert "[Worker]" in caplog.text
        with patch(_VERIFY, return_value=_claims(_OLD)) as verify:
            assert await gate("Bearer t") is False
            assert await gate(None) is False
        verify.assert_not_called()

    async def test_empty_locally_is_open(self):
        gate = build_oidc_gate(frozenset(), on_cloud_run=False)
        assert await gate(None) is True


class TestIndexerGate:
    async def test_built_from_drive_invoker_plus_transition(self):
        accepted = parse_sa_emails(_NEW, f"{_OLD},-")
        gate = worker_oidc_gate(accepted, ["https://idx"], on_cloud_run=True)
        with patch(_VERIFY, return_value=_claims(_OLD, aud="https://idx")):
            assert await gate("Bearer t") is True
        with patch(_VERIFY, return_value=_claims(_RELAY, aud="https://idx")):
            assert await gate("Bearer t") is False

    async def test_audience_pin_unchanged(self):
        gate = worker_oidc_gate({_OLD}, ["https://idx"], on_cloud_run=True)
        with patch(_VERIFY, return_value=_claims(_OLD, aud="https://other")):
            assert await gate("Bearer t") is False

    async def test_empty_on_cloud_run_closed_locally_open(self):
        assert await worker_oidc_gate(frozenset(), on_cloud_run=True)(None) is False
        assert await worker_oidc_gate(frozenset(), on_cloud_run=False)(None) is True

    def test_str_is_a_type_error(self):
        with pytest.raises(TypeError):
            worker_oidc_gate(_OLD)  # type: ignore[arg-type]
