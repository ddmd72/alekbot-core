"""The main service's document service (EXTERNAL_FILES_RFC §4.4, §4.5; U3 core): one parser child serving parser and
splitter at MAIN_PARSER_MAX_CHILDREN, the reader over the given context builder with a plain PromptBuilder, and the
transcriber, cache and quota passed through. Built offline: no child runs."""
import pytest

from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.composition.document_service_setup import MAIN_PARSER_MAX_CHILDREN, build_document_service
from src.composition.drive_reader_runner import DriveReaderRunner
from src.services.document_service import DocumentService
from src.services.prompt_builder import PromptBuilder

CONTEXT, USERS, ASSEMBLY, QUOTA, TRANSCRIBER, CACHE = (object() for _ in range(6))


def _build(parser_user=None) -> DocumentService:
    return build_document_service(context_builder=CONTEXT, users=USERS, assembly_service=ASSEMBLY, quota=QUOTA,
                                  transcriber=TRANSCRIBER, transcript_cache=CACHE, parser_user=parser_user)


@pytest.fixture
def local(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)


def test_one_parser_child_is_parser_and_splitter(local):
    service = _build()
    assert isinstance(service._parser, SubprocessContentExtractor)
    assert service._splitter is service._parser
    assert service._parser._slots._value == MAIN_PARSER_MAX_CHILDREN == 1


def test_the_reader_runs_over_the_main_context_builder_with_a_plain_prompt_builder(local):
    service = _build()
    reader = service._reader
    assert isinstance(reader, DriveReaderRunner)
    assert reader._context_builder is CONTEXT and reader._user_repo is USERS and reader._quota_service is QUOTA
    assert reader._splitter is service._parser
    assert type(reader._prompt_builder) is PromptBuilder          # never the indexer's _DrivePromptBuilder
    assert reader._prompt_builder.assembly_service is ASSEMBLY


def test_transcriber_cache_and_quota_are_passed_through(local):
    service = _build()
    assert (service._transcriber, service._transcript_cache, service._quota) == (TRANSCRIBER, CACHE, QUOTA)


def test_cloud_run_without_the_parser_user_refuses_to_build(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "alek-bot")
    with pytest.raises(RuntimeError):
        _build(parser_user=None)
