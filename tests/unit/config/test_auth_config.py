"""
Unit tests for AuthConfig — fail-closed guard on the session-signing secret.

The dev placeholder secret lives in source control (public after release). It
must never sign real Cabinet JWTs. validate() refuses to start when it would be
used in a deployed environment (Cloud Run sets K_SERVICE).
"""
import os
from unittest.mock import patch

import pytest

from src.config.auth import AuthConfig, _DEV_DEFAULT_SESSION_SECRET


# Firebase config required so validate() reaches the session-secret check.
_COMPLETE = {
    "FIREBASE_PROJECT_ID": "test",
    "FIREBASE_WEB_API_KEY": "key",
    "GOOGLE_OAUTH_CLIENT_ID": "client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
    "OAUTH_REDIRECT_URI": "https://example.com/callback",
}


class TestSessionSecretFailClosed:

    def test_rejects_dev_default_secret_when_deployed(self):
        # Deployed (K_SERVICE) + OAUTH_SESSION_SECRET unset → placeholder → refuse.
        env = {**_COMPLETE, "K_SERVICE": "alek-bot-dev"}
        with patch.dict(os.environ, env, clear=True):
            config = AuthConfig()
            assert config.oauth_session_secret == _DEV_DEFAULT_SESSION_SECRET
            with pytest.raises(ValueError, match="dev placeholder"):
                config.validate()

    def test_allows_dev_default_secret_locally(self):
        # No K_SERVICE → local laptop run → placeholder acceptable.
        with patch.dict(os.environ, _COMPLETE, clear=True):
            config = AuthConfig()
            config.validate()  # must not raise

    def test_passes_real_secret_when_deployed(self):
        env = {
            **_COMPLETE,
            "K_SERVICE": "alek-bot-dev",
            "OAUTH_SESSION_SECRET": "a-real-secret-from-secret-manager-32chars",
        }
        with patch.dict(os.environ, env, clear=True):
            config = AuthConfig()
            config.validate()  # must not raise

    def test_short_secret_still_rejected(self):
        env = {**_COMPLETE, "OAUTH_SESSION_SECRET": "too-short"}
        with patch.dict(os.environ, env, clear=True):
            config = AuthConfig()
            with pytest.raises(ValueError, match="at least 32 characters"):
                config.validate()
