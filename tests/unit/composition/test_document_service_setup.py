"""The main service's document service (EXTERNAL_FILES_RFC §4.4, §4.5; U3 core): the given parse adapter serving parser and
splitter, the reader over the given context builder with a plain PromptBuilder, and the
transcriber, cache and quota passed through. Built offline: no child runs."""
import pytest

from src.composition.document_service_setup import build_document_service
from src.composition.drive_reader_runner import DriveReaderRunner
from src.services.document_service import DocumentService
from src.services.prompt_builder import PromptBuilder

CONTEXT, USERS, ASSEMBLY, QUOTA, TRANSCRIBER, CACHE, PARSER = (object() for _ in range(7))


def _build() -> DocumentService:
    return build_document_service(context_builder=CONTEXT, users=USERS, assembly_service=ASSEMBLY, quota=QUOTA,
                                  transcriber=TRANSCRIBER, transcript_cache=CACHE, parser=PARSER)


@pytest.fixture
def local(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)


def test_the_reader_runs_over_the_main_context_builder_with_a_plain_prompt_builder(local):
    service = _build()
    reader = service._reader
    assert isinstance(reader, DriveReaderRunner)
    assert reader._context_builder is CONTEXT and reader._user_repo is USERS and reader._quota_service is QUOTA
    assert reader._splitter is PARSER
    assert type(reader._prompt_builder) is PromptBuilder          # never the indexer's _DrivePromptBuilder
    assert reader._prompt_builder.assembly_service is ASSEMBLY


def test_transcriber_cache_and_quota_are_passed_through(local):
    service = _build()
    assert (service._transcriber, service._transcript_cache, service._quota) == (TRANSCRIBER, CACHE, QUOTA)
