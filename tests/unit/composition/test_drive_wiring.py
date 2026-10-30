"""The drive reaches FileManagementAgent only through composition, with the drive timeout."""
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.composition import user_agent_factory
from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository
from tests.support.aes_gcm_cipher import AesGcmTestCipher


def test_factory_wires_drive_and_timeout():
    src = inspect.getsource(user_agent_factory.UserAgentFactory._build_file_management)
    for kw in ("drive_service=", "localization=", "language_service=", "timeout_ms=120_000"):
        assert kw in src


def test_agent_accepts_drive_kwargs():
    params = inspect.signature(FileManagementAgent.__init__).parameters
    assert {"drive_service", "localization", "language_service"} <= set(params)


def _container(monkeypatch, **extra):
    monkeypatch.setenv("APP_ENV", "test")
    config = {
        "GEMINI_API_KEY": "k", "ANTHROPIC_API_KEY": "k",
        "GOOGLE_OAUTH_CLIENT_ID": "k", "GOOGLE_OAUTH_CLIENT_SECRET": "k",
        "CONSOLIDATION": ConsolidationSettings(), "GCS_MEDIA_BUCKET": "",
        **extra,
    }
    return ServiceContainer(
        cipher=AesGcmTestCipher(),
        config=config, db_client=MagicMock(), env_config=EnvironmentConfig(),
        account_repo=AsyncMock(spec=AccountRepository),
    )


class TestContainerDriveWiring:
    def test_absent_without_microsoft_app_registration(self, monkeypatch):
        c = _container(monkeypatch)
        assert c.user_drive is None and c.user_drive_service is None
        assert c.agent_services()["user_drive_service"] is None

    def test_present_with_microsoft_app_registration(self, monkeypatch):
        c = _container(monkeypatch, MICROSOFT_TODO_CLIENT_ID="cid", MICROSOFT_TODO_CLIENT_SECRET="sec",
                       GCS_MEDIA_BUCKET="bucket")
        assert c.user_drive is not None and c.user_drive_service is not None
        # Both consumers hang off the same adapter: conversion reads, the agent mutates.
        assert c.file_conversion_service._drive is c.user_drive
        assert c.user_drive_service._drive is c.user_drive
        assert c.agent_services()["user_drive_service"] is c.user_drive_service

    def test_both_or_neither(self, monkeypatch):
        absent = _container(monkeypatch, GCS_MEDIA_BUCKET="bucket")
        assert absent.file_conversion_service._drive is None and absent.user_drive_service is None


def test_the_drive_connects_through_the_one_redirect_key(monkeypatch):
    """The drive connects through the one callback of every realm (MULTI_DRIVE_RFC §5.2, E4): its own
    ONEDRIVE_REDIRECT_URI is gone, and OAUTH_CONNECT_REDIRECT_URI is read by AuthConfig (load_settings' dict, then
    the environment), so the deploy's value is never a silently dead config.get() of an unlisted key."""
    from src.config import settings
    from src.config.auth import AuthConfig
    assert "ONEDRIVE_REDIRECT_URI" not in inspect.getsource(settings)
    monkeypatch.setenv("OAUTH_CONNECT_REDIRECT_URI", "https://service.example/auth/connect/callback")
    assert AuthConfig({}).oauth_connect_redirect_uri == "https://service.example/auth/connect/callback"
