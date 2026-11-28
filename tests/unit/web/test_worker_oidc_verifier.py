"""
Unit tests for verify_worker_oidc.

Mock boundary: google.oauth2.id_token.verify_oauth2_token (the SDK call that
performs the cryptographic check). We never reach Google's JWKS endpoint — we
assert the verifier's decision logic around that call: header parsing, identity
(email / email_verified) checks, fail-closed behaviour, and that audience is NOT
pinned (callers use inconsistent audiences — see verifier docstring).
"""
from unittest.mock import patch

from src.web.worker_oidc_verifier import verify_worker_oidc


_SA = "alek-bot@my-project.iam.gserviceaccount.com"
# The verifier takes the accepted set (IAM least-privilege, Step 0 C1); a single identity is a one-item set.
_ACCEPTED = frozenset({_SA})


def _good_claims(**overrides):
    claims = {"email": _SA, "email_verified": True}
    claims.update(overrides)
    return claims


class TestVerifyWorkerOidc:
    def test_valid_token_matching_sa_and_verified_email(self):
        with patch(
            "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token",
            return_value=_good_claims(),
        ) as mock_verify:
            assert verify_worker_oidc("Bearer good.jwt.token", _ACCEPTED) is True
        # audience must NOT be pinned — callers use inconsistent audiences.
        assert mock_verify.call_args.kwargs.get("audience") is None

    def test_wrong_service_account_email_rejected(self):
        with patch(
            "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token",
            return_value=_good_claims(email="attacker@evil.iam.gserviceaccount.com"),
        ):
            assert verify_worker_oidc("Bearer good.jwt.token", _ACCEPTED) is False

    def test_email_not_verified_rejected(self):
        with patch(
            "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token",
            return_value=_good_claims(email_verified=False),
        ):
            assert verify_worker_oidc("Bearer good.jwt.token", _ACCEPTED) is False

    def test_email_verified_absent_rejected(self):
        with patch(
            "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token",
            return_value={"email": _SA},
        ):
            assert verify_worker_oidc("Bearer good.jwt.token", _ACCEPTED) is False

    def test_token_for_any_audience_accepted_when_sa_matches(self):
        # A Cloud Scheduler token (audience = bare run.app URL) and a Cloud Tasks
        # token (audience = .../worker) both carry the same SA — both must pass.
        with patch(
            "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token",
            return_value=_good_claims(aud="https://alek-bot-dev.run.app"),
        ):
            assert verify_worker_oidc("Bearer scheduler.jwt.token", _ACCEPTED) is True

    def test_missing_header_rejected(self):
        assert verify_worker_oidc(None, _ACCEPTED) is False

    def test_malformed_header_without_bearer_rejected(self):
        assert verify_worker_oidc("good.jwt.token", _ACCEPTED) is False

    def test_empty_bearer_token_rejected(self):
        assert verify_worker_oidc("Bearer    ", _ACCEPTED) is False

    def test_verifier_exception_is_caught_and_rejected(self):
        # Bad signature / expired → verify_oauth2_token raises.
        with patch(
            "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token",
            side_effect=ValueError("Token has expired"),
        ):
            assert verify_worker_oidc("Bearer bad.jwt.token", _ACCEPTED) is False


class TestTransitionAcceptanceLog:
    _TRANSITION = "old-sa@my-project.iam.gserviceaccount.com"

    def _verify(self, email, caplog):
        import logging
        caplog.set_level(logging.INFO)
        with patch(
            "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token",
            return_value=_good_claims(email=email),
        ):
            return verify_worker_oidc(
                "Bearer good.jwt.token", {_SA, self._TRANSITION}, transition_emails={self._TRANSITION})

    def _lines(self, caplog):
        return [r.getMessage() for r in caplog.records if "transition list" in r.getMessage()]

    def test_a_call_accepted_through_the_transition_list_logs_one_info_line(self, caplog):
        assert self._verify(self._TRANSITION, caplog) is True
        lines = self._lines(caplog)
        assert len(lines) == 1
        assert self._TRANSITION not in lines[0]
        assert [r.levelname for r in caplog.records if "transition list" in r.getMessage()] == ["INFO"]

    def test_a_regular_acceptance_logs_no_transition_line(self, caplog):
        assert self._verify(_SA, caplog) is True
        assert self._lines(caplog) == []

    def test_a_rejected_call_logs_no_transition_line(self, caplog):
        assert self._verify("other@my-project.iam.gserviceaccount.com", caplog) is False
        assert self._lines(caplog) == []
