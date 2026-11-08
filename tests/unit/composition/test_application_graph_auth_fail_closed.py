"""Startup guard: no start with a placeholder Cabinet secret or a localhost OAuth callback on Cloud Run.

`build_application_graph` calls `AuthConfig.validate()` next to the cipher and digest key checks, so a missing or
short `OAUTH_SESSION_SECRET`, or a localhost `OAUTH_REDIRECT_URI` under `K_SERVICE`, raises before any repository
exists. The file-link token service and the Cabinet session service share the one configured secret.
"""
from typing import Any

import pytest

from src.composition.application_graph import build_application_graph
from src.config.auth import AuthConfig
from src.services.file_access_token_service import FileAccessTokenInvalid, FileAccessTokenService
from tests.support import prod_env

PLACEHOLDER = "dev-secret-change-in-production-must-be-32-chars-long"


class UntouchableDb:
    """A Firestore client the graph must not reach: any attribute access fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"build_application_graph touched the database ({name}) before refusing to start")


def _settings(monkeypatch, **overrides):
    config, _ = prod_env.prod_settings(monkeypatch)
    for key, value in overrides.items():
        if value is None:
            config.pop(key, None)
            monkeypatch.delenv(key, raising=False)
        else:
            config[key] = value
    return config


def test_a_missing_session_secret_on_cloud_run_refuses_the_start(monkeypatch):
    config = _settings(monkeypatch, OAUTH_SESSION_SECRET=None)

    with pytest.raises(ValueError, match="dev placeholder"):
        build_application_graph(config, UntouchableDb())


def test_the_placeholder_secret_on_cloud_run_refuses_the_start(monkeypatch):
    config = _settings(monkeypatch, OAUTH_SESSION_SECRET=PLACEHOLDER)

    with pytest.raises(ValueError, match="dev placeholder"):
        build_application_graph(config, UntouchableDb())


def test_a_localhost_callback_on_cloud_run_refuses_the_start(monkeypatch):
    config = _settings(monkeypatch, OAUTH_REDIRECT_URI="http://localhost:8080/auth/callback")

    with pytest.raises(ValueError, match="localhost"):
        build_application_graph(config, UntouchableDb())


def test_a_short_session_secret_refuses_the_start(monkeypatch):
    config = _settings(monkeypatch, OAUTH_SESSION_SECRET="s" * 31)

    with pytest.raises(ValueError, match="at least 32 characters"):
        build_application_graph(config, UntouchableDb())


async def test_prod_shaped_settings_build_and_both_services_sign_with_the_configured_secret(monkeypatch):
    offline = prod_env.install_offline_services(monkeypatch)
    from src.config.settings import load_settings
    config = load_settings()
    secret = config["OAUTH_SESSION_SECRET"]
    assert secret and secret != PLACEHOLDER

    graph = build_application_graph(config, offline.db)
    try:
        token = graph.file_token_service.mint("docs/x.pdf", "u1")
        assert FileAccessTokenService(secret_key=secret).verify(token).key == "docs/x.pdf"
        with pytest.raises(FileAccessTokenInvalid):
            FileAccessTokenService(secret_key=PLACEHOLDER).verify(token)
        assert graph.session_service.secret_key == secret
        assert graph.auth_config.oauth_session_secret == secret
        assert isinstance(graph.auth_config, AuthConfig)
    finally:
        await graph.shutdown()
