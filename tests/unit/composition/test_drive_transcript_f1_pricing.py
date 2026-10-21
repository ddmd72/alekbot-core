"""Final-review fix wave F1 (USER_DRIVE_INDEXING_RFC §27.5, Δ156): the reservation's planning prices are the prices of the
model the composition actually runs, and a collection whose text input passes the reserved allowance says so once."""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.gemini_adapter import GeminiAdapter
from src.composition.drive_transcript_runner import DriveTranscriptRunner
from src.domain.batch_transcription import BatchRef, BatchResult
from src.domain.billing import BatchUsage, batch_transcription_input_usd, batch_transcription_output_usd
from src.domain.drive_index_params import TRANSCRIBE_WINDOW_PROMPT_TOKENS
from src.domain.user import PerformanceTier
from src.ports.batch_transcription_port import BatchTranscriptionPort
from src.ports.quota_service import QuotaService
from tests.unit.agents.drive_model_fakes import prompt_builder

GOOD = json.dumps({"language_codes": ["en"], "speakers": 1, "no_speech": False,
                   "segments": [{"start": "00:00", "speaker": None, "text": "hello"}]})
RAN = GeminiAdapter.MODEL_TIERS[PerformanceTier.BALANCED]   # what drive_model_setup passes to GeminiBatchAdapter


def test_the_planning_prices_are_the_prices_of_the_model_the_composition_runs():
    """billing.batch_transcription_* price the reservation with a fixed model id; the composition runs the adapter's
    BALANCED tier (Δ156). A tier bump without the planning model moving would reserve at the wrong rates."""
    million = 1_000_000
    assert BatchUsage(model=RAN).is_priced()
    assert batch_transcription_input_usd(million) == pytest.approx(
        BatchUsage(model=RAN, text_input_tokens=million).cost())
    assert batch_transcription_output_usd(million) == pytest.approx(
        BatchUsage(model=RAN, output_tokens=million).cost())


def test_the_composition_builds_the_batch_adapter_on_that_model():
    from src.composition.drive_model_setup import build_drive_transcription
    models = SimpleNamespace(prompt_builder=prompt_builder(), quota=AsyncMock(spec=QuotaService))
    stage = build_drive_transcription(enabled=True, repo=MagicMock(), drive=MagicMock(), models=models,
                                      keys={"GEMINI_API_KEY": "test-key"}, parser_user=None, monthly_cap_usd=10.0)
    assert stage is not None and stage._batch._model == RAN


def _runner(results):
    port = AsyncMock(spec=BatchTranscriptionPort)
    port.collect.return_value = results
    return DriveTranscriptRunner(port, prompt_builder(), AsyncMock(spec=QuotaService))


def _result(key, text_tokens):
    return BatchResult(key=key, text=GOOD, finish_reason="STOP",
                       usage=BatchUsage(model=RAN, audio_input_tokens=1_000, text_input_tokens=text_tokens,
                                        output_tokens=100))


async def test_text_input_over_the_allowance_logs_one_error_per_collect(caplog):
    over = TRANSCRIBE_WINDOW_PROMPT_TOKENS + 1
    runner = _runner({"a:0": _result("a:0", over), "a:1": _result("a:1", over + 5), "a:2": _result("a:2", 900)})
    with caplog.at_level(logging.ERROR):
        await runner.collect(BatchRef(name="batches/b", keys=("a:0", "a:1", "a:2")), account_id="acct")
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and "allowance" in r.getMessage()]
    assert len(errors) == 1
    assert "2 of 3" in errors[0] and str(over + 5) in errors[0] and "hello" not in errors[0]


async def test_text_input_within_the_allowance_logs_nothing(caplog):
    runner = _runner({"a:0": _result("a:0", TRANSCRIBE_WINDOW_PROMPT_TOKENS)})
    with caplog.at_level(logging.ERROR):
        await runner.collect(BatchRef(name="batches/b", keys=("a:0",)), account_id="acct")
    assert not [r for r in caplog.records if "allowance" in r.getMessage()]
