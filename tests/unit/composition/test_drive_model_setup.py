"""The indexer's model wiring (USER_DRIVE_INDEXING_RFC §24.2, §24.9): Claude and OpenAI registered by key (stripped),
never Gemini; the extractor registry keyed by the EXTRACTOR_CHAINS ids; both runners share one context builder,
prompt builder and quota service; the shared prompt-assembly builder."""
from unittest.mock import MagicMock

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.adapters.openai_adapter import OpenAIAdapter
from src.composition import drive_model_setup
from src.composition.drive_model_setup import build_drive_model_extractors, build_drive_model_registry
from src.composition.drive_reader_runner import DriveReaderRunner
from src.composition.drive_triage_runner import DriveTriageRunner
from src.composition.prompt_assembly_setup import build_prompt_assembly_service
from src.config.environment import EnvironmentConfig
from src.domain.drive_extraction import PARSER, READER
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService


def test_both_providers_registered_and_no_gemini():
    registry = build_drive_model_registry({"ANTHROPIC_API_KEY": "a\n", "OPENAI_API_KEY": " o "})
    assert isinstance(registry.get("claude"), ClaudeAdapter) and isinstance(registry.get("openai"), OpenAIAdapter)
    assert registry.get("openai").client.api_key == "o"
    assert set(registry.list_available()) == {"claude", "openai"}


def test_a_missing_key_leaves_its_provider_out(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(drive_model_setup, "logger", log)
    registry = build_drive_model_registry({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": ""})
    assert set(registry.list_available()) == {"claude"}
    assert "OPENAI_API_KEY" in log.warning.call_args.args[0]


def test_extractor_registry_and_triage():
    parser = MagicMock()
    extractors, triage = build_drive_model_extractors(
        parser=parser, users=MagicMock(), account_repo=MagicMock(), assembly_service=MagicMock(),
        keys={"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}, alert_sink=None)
    assert set(extractors) == {PARSER, READER} and extractors[PARSER] is parser
    reader = extractors[READER]
    assert isinstance(reader, DriveReaderRunner) and isinstance(triage, DriveTriageRunner)
    assert reader._splitter is parser
    assert reader._context_builder is triage._context_builder
    assert reader._prompt_builder is triage._prompt_builder
    assert isinstance(reader._quota_service, FirestoreQuotaService) and reader._quota_service is triage._quota_service


def test_prompt_assembly_builder_uses_the_environment_collections():
    env = EnvironmentConfig()
    service, profiles = build_prompt_assembly_service(MagicMock(), env)
    assert isinstance(service, PromptAssemblyService)
    assert profiles.profiles_collection == env.domain_prompt_profiles_collection
