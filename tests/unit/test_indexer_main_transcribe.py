"""indexer_main wiring of the transcript stage (USER_DRIVE_INDEXING_RFC §27.13; plan Task 14B; G26, G13, T-R3): the kill
switch, the stage built over the BALANCED Gemini model, the capped monthly dollars shared by the stage and the tick, and
an object graph that is today's while the switch is off. Google clients are replaced; nothing leaves the process."""
import logging
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.gemini_adapter import GeminiAdapter
from src.composition import drive_indexing_setup, drive_model_setup
from src.domain.drive_index_params import TRANSCRIBE_USD_PER_MONTH
from src.domain.user import PerformanceTier


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def wired(monkeypatch):
    captured = {"batch": []}
    monkeypatch.delenv("K_SERVICE", raising=False)
    for key in ("DRIVE_TRANSCRIBE_ENABLED", "DRIVE_TRANSCRIBE_USD_CAP"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       # the cipher's key name (SECRETS_AT_REST_RFC §2.6, I6); build_cipher calls no KMS API
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    real_service = indexer_main.DriveIndexingService
    real_batch = drive_model_setup.GeminiBatchAdapter

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return real_service(*args, **kwargs)

    def batch(*args, **kwargs):
        captured["batch"].append(kwargs)
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(indexer_main, "DriveIndexingService", capture)
    monkeypatch.setattr(drive_model_setup, "GeminiBatchAdapter", batch)
    return captured


@pytest.mark.parametrize("value,expected", [(None, False), ("", False), ("off", False), ("0", False), ("no", False),
                                            ("maybe", False), ("on", True), ("ON", True), (" 1 ", True),
                                            ("true", True), ("yes", True)])
def test_the_switch_is_on_only_for_the_listed_values(value, expected):
    assert indexer_main.transcribe_enabled(value) is expected


def test_switch_off_builds_no_stage(wired):
    indexer_main.build_app()
    assert wired["transcripts"] is None and wired["transcribe_enabled"] is False
    assert wired["batch"] == []   # no Gemini batch client either: the object graph is today's


def test_indexer_wires_stage_with_balanced_model(wired, monkeypatch):
    monkeypatch.setenv("DRIVE_TRANSCRIBE_ENABLED", "on")
    indexer_main.build_app()
    assert wired["transcripts"] is not None and wired["transcribe_enabled"] is True
    assert [b["model"] for b in wired["batch"]] == [GeminiAdapter.MODEL_TIERS[PerformanceTier.BALANCED]]
    assert "lite" not in wired["batch"][0]["model"]   # G26 / D44: never flash-lite
    assert wired["transcribe_cap_usd"] == TRANSCRIBE_USD_PER_MONTH


def test_without_a_gemini_key_the_stage_is_not_built_and_an_error_is_logged(wired, monkeypatch, caplog):
    monkeypatch.setenv("DRIVE_TRANSCRIBE_ENABLED", "on")
    monkeypatch.setenv("GEMINI_API_KEY", " ")
    with caplog.at_level(logging.ERROR):
        indexer_main.build_app()
    assert wired["transcripts"] is None and wired["transcribe_enabled"] is False
    assert any("GEMINI_API_KEY" in r.message and r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("raw,expected", [("10", 10.0), ("50", TRANSCRIBE_USD_PER_MONTH), ("", TRANSCRIBE_USD_PER_MONTH),
                                          ("junk", TRANSCRIBE_USD_PER_MONTH), ("-3", TRANSCRIBE_USD_PER_MONTH)])
def test_cap_override_only_lowers(wired, monkeypatch, raw, expected):
    """One effective value, read once, reaches both the stage and the tick's cap math; the knob never raises $20."""
    monkeypatch.setenv("DRIVE_TRANSCRIBE_ENABLED", "on")
    monkeypatch.setenv("DRIVE_TRANSCRIBE_USD_CAP", raw)
    indexer_main.build_app()
    assert wired["transcribe_cap_usd"] == expected
    assert wired["transcripts"]._cap == expected


def test_the_cap_knob_without_the_switch_still_reaches_the_tick(wired, monkeypatch):
    monkeypatch.setenv("DRIVE_TRANSCRIBE_USD_CAP", "5")
    indexer_main.build_app()
    assert wired["transcripts"] is None and wired["transcribe_cap_usd"] == 5.0
