"""The transcript stage decodes recordings only through the media port it is given (UNTRUSTED_CONTENT_SANDBOX_RFC §7 S5;
S5 design §2.7): the sandbox's `SandboxMediaAdapter`, never an in-process extractor or the loopback relay."""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.adapters.sandbox_client.sandbox_media_adapter import SandboxMediaAdapter
from src.composition import drive_model_setup
from src.composition.drive_model_setup import build_drive_transcription
from src.composition.sandbox_media_setup import build_sandbox_media
from src.ports.quota_service import QuotaService
from src.services.prompt_builder import PromptBuilder


def _models():
    return SimpleNamespace(prompt_builder=MagicMock(spec=PromptBuilder), quota=AsyncMock(spec=QuotaService))


def test_the_stage_reads_recordings_through_the_sandbox_media_adapter():
    media = build_sandbox_media("https://alek-sandbox-light.test.example")
    stage = build_drive_transcription(enabled=True, repo=MagicMock(), drive=MagicMock(), models=_models(),
                                      keys={"GEMINI_API_KEY": "test-key"}, media=media, monthly_cap_usd=10.0)
    assert stage is not None and stage._media is media and isinstance(stage._media, SandboxMediaAdapter)


def test_the_media_port_is_required_and_no_parser_user_is_taken():
    params = inspect.signature(build_drive_transcription).parameters
    assert params["media"].default is inspect.Parameter.empty
    assert "parser_user" not in params


def test_no_in_process_media_path_is_reachable_from_the_composition():
    source = inspect.getsource(drive_model_setup)
    for name in ("relay_child_url", "SubprocessMediaExtractor", "media_relay", "media_extractor", "media_launch"):
        assert name not in source and not hasattr(drive_model_setup, name), name


def test_switch_off_builds_no_stage_whatever_the_media():
    assert build_drive_transcription(enabled=False, repo=MagicMock(), drive=MagicMock(), models=_models(),
                                     keys={"GEMINI_API_KEY": "test-key"}, media=MagicMock(),
                                     monthly_cap_usd=10.0) is None
