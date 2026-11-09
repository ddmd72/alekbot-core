"""scripts/infrastructure/check_env_drift.py: the pure comparison, the service list, the report (names only)."""
from pathlib import Path

from scripts.infrastructure import check_env_drift as ced

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_dropped_names_found_sorted():
    assert ced.dropped_names({"A", "SWITCH_B", "SWITCH_A"}, {"A"}) == ["SWITCH_A", "SWITCH_B"]


def test_added_names_ignored():
    assert ced.dropped_names({"A"}, {"A", "NEW"}) == []


def test_no_drop_is_empty():
    assert ced.dropped_names(["A", "B"], ["B", "A"]) == []


def test_report_lists_names_and_never_values():
    previous = {"SWITCH_X": "secret-value-123", "KEEP": "k"}
    current = {"KEEP": "k"}
    report = ced.drift_report({"svc-a": ced.dropped_names(previous, current)})
    assert "SWITCH_X" in report and "svc-a" in report and "already happened" in report
    assert "secret-value-123" not in report


def test_deployed_services_from_real_config_skips_jobs():
    services = ced.deployed_services(REPO_ROOT / "cloudbuild-dev.yaml")
    assert "alek-bot-dev" in services
    assert "alek-research-job-dev" not in services


def test_main_service_sets_voice_switches():
    text = (REPO_ROOT / "cloudbuild-dev.yaml").read_text(encoding="utf-8")
    assert "VOICE_CONTROL_CHANNEL=on,VOICE_MCP_TOOLS=on" in text
