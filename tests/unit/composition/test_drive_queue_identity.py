"""IAM least-privilege Step 0 (C3): the drive queue sends as DRIVE_INVOKER_SA_EMAIL, falling back to
SERVICE_ACCOUNT_EMAIL (today's behaviour while the new secret is absent)."""
import pytest

from src.composition import drive_indexing_setup as setup
from src.config.environment import EnvironmentConfig


class _Q:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DRIVE_INDEXER_URL", "https://indexer.example.invalid")
    monkeypatch.setattr(setup, "GcpTaskQueue", _Q)

    def _build(**config):
        return setup.build_drive_task_queue({"GOOGLE_CLOUD_PROJECT": "p", **config}, EnvironmentConfig())
    return _build


def test_prefers_drive_invoker(build):
    q = build(SERVICE_ACCOUNT_EMAIL="legacy@x", DRIVE_INVOKER_SA_EMAIL="drive@x")
    assert q.kwargs["service_account_email"] == "drive@x"


def test_falls_back_to_service_account_email_when_unset(build):
    assert build(SERVICE_ACCOUNT_EMAIL="legacy@x").kwargs["service_account_email"] == "legacy@x"
    assert build(SERVICE_ACCOUNT_EMAIL="legacy@x", DRIVE_INVOKER_SA_EMAIL=None
                 ).kwargs["service_account_email"] == "legacy@x"
