"""A failed OIDC verification must never write token-derived text to the logs.

google-auth embeds the offending token in some messages (e.g. ``MalformedError("Wrong number
of segments in token: <token>")``); a replayable ID token in Cloud Logging is a credential leak.
"""
import logging
from unittest.mock import patch

from google.auth.exceptions import MalformedError

from src.web.worker_oidc_verifier import verify_worker_oidc

SENTINEL = "SENTINEL-TOKEN-aaa.bbb.ccc-do-not-log"


def test_verification_failure_logs_class_name_not_token(caplog):
    err = MalformedError(f"Wrong number of segments in token: {SENTINEL}")
    with patch("src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token", side_effect=err):
        with caplog.at_level(logging.DEBUG):
            result = verify_worker_oidc(f"Bearer {SENTINEL}", {"sa@example.com"})

    assert result is False
    assert SENTINEL not in caplog.text
    assert "MalformedError" in caplog.text
