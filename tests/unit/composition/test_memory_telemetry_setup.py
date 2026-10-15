"""build_memory_telemetry: env switches and process identity."""
import pytest

from src.composition.memory_telemetry_setup import (
    build_memory_telemetry, memory_telemetry_enabled, sample_interval_from_env,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("MEMORY_TELEMETRY", "MEMORY_SAMPLE_INTERVAL_S", "K_SERVICE", "K_REVISION",
              "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "No"])
def test_switched_off(monkeypatch, value):
    monkeypatch.setenv("MEMORY_TELEMETRY", value)
    assert not memory_telemetry_enabled()
    assert build_memory_telemetry("alek-core") is None


def test_default_on_with_local_identity():
    svc = build_memory_telemetry("alek-core")
    assert svc is not None and svc._service == "alek-core" and svc._revision == "local"
    assert svc._base_interval_s == 15.0


def test_cloud_run_service_identity(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "alek-bot-dev")
    monkeypatch.setenv("K_REVISION", "alek-bot-dev-00395-b46")
    svc = build_memory_telemetry("alek-core")
    assert (svc._service, svc._revision) == ("alek-bot-dev", "alek-bot-dev-00395-b46")


def test_cloud_run_job_identity(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_JOB", "alek-research-job-dev")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "alek-research-job-dev-abc12")
    svc = build_memory_telemetry("alek-research-job")
    assert (svc._service, svc._revision) == ("alek-research-job-dev", "alek-research-job-dev-abc12")


@pytest.mark.parametrize("raw,expected", [("30", 30.0), ("0.2", 1.0), ("junk", 15.0), ("", 15.0)])
def test_interval_parsing_is_clamped_and_forgiving(monkeypatch, raw, expected):
    monkeypatch.setenv("MEMORY_SAMPLE_INTERVAL_S", raw)
    assert sample_interval_from_env() == expected
