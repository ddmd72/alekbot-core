from google.auth.exceptions import DefaultCredentialsError

from src.adapters.sandbox_client.metadata_id_token_provider import _is_transient


def test_failed_ping_on_a_cloud_run_job_is_transient(monkeypatch):
    for name in ("K_SERVICE", "K_JOB", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(name, raising=False)
    assert not _is_transient(DefaultCredentialsError("no creds"))
    monkeypatch.setenv("CLOUD_RUN_JOB", "job")
    assert _is_transient(DefaultCredentialsError("no creds"))


def test_k_job_is_not_a_cloud_run_signal(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv("K_JOB", "job")
    assert not _is_transient(DefaultCredentialsError("no creds"))
