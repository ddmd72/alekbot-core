"""GCS_WORKSPACE_BUCKET is required configuration, declared in load_settings() (AGENT_WORKSPACE_RFC §5.5)."""
from src.config import settings as settings_module


def _load(monkeypatch, value=None):
    monkeypatch.setattr(settings_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")  # no Secret Manager lookups in a unit test
    if value is None:
        monkeypatch.delenv("GCS_WORKSPACE_BUCKET", raising=False)
    else:
        monkeypatch.setenv("GCS_WORKSPACE_BUCKET", value)
    return settings_module.load_settings()


def test_read_from_the_environment(monkeypatch):
    assert _load(monkeypatch, "ws-bucket")["GCS_WORKSPACE_BUCKET"] == "ws-bucket"


def test_empty_by_default(monkeypatch):
    assert _load(monkeypatch)["GCS_WORKSPACE_BUCKET"] == ""
