"""The composition gives the transcript stage's media extractor the loopback relay as its child-URL opener
(GOOGLE_DRIVE_RFC G2, design §1.6)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.adapters.drive_parser.media_relay import relay_child_url
from src.composition.drive_model_setup import build_drive_transcription
from src.ports.quota_service import QuotaService
from src.services.prompt_builder import PromptBuilder


def test_the_transcript_stage_reads_media_through_the_relay(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    models = SimpleNamespace(prompt_builder=MagicMock(spec=PromptBuilder), quota=AsyncMock(spec=QuotaService))
    stage = build_drive_transcription(enabled=True, repo=MagicMock(), drive=MagicMock(), models=models,
                                      keys={"GEMINI_API_KEY": "test-key"}, parser_user=None, monthly_cap_usd=10.0)
    assert stage is not None
    assert stage._media._child_url is relay_child_url
