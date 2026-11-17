"""The document service's outside parts (`DocumentBackends`) and the container that builds the document service over them
(EXTERNAL_FILES_RFC §4.4, P21): the graph builds the parse adapter and the transcriber before the container, the container
builds the document service with its own context builder and hands it to the file conversion service at construction."""
import dataclasses
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_transcript_cache import FirestoreTranscriptCache
from src.adapters.openai_transcription_adapter import OpenAITranscriptionAdapter
from src.composition import service_container as service_container_module
from src.composition.deploy_topology import build_deploy_topology
from src.composition.document_service_setup import DocumentBackends, build_document_backends
from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository
from src.services.document_service import DocumentService
from tests.support.aes_gcm_cipher import AesGcmTestCipher

PARSER = object()


def test_with_an_openai_key_the_backends_hold_the_parser_and_a_transcriber_on_the_pinned_model(monkeypatch):
    monkeypatch.setenv("OPENAI_TRANSCRIPTION_MODEL", "pinned-transcription-model")

    backends = build_document_backends({"OPENAI_API_KEY": "sk-test"}, parser=PARSER)

    assert backends.parser is PARSER
    assert isinstance(backends.transcriber, OpenAITranscriptionAdapter)
    assert backends.transcriber.model == "pinned-transcription-model"


def test_without_an_openai_key_there_is_no_transcriber_and_one_info_line_says_so(caplog):
    with caplog.at_level(logging.INFO):
        backends = build_document_backends({}, parser=PARSER)

    assert backends == DocumentBackends(parser=PARSER, transcriber=None)
    assert any(r.levelno == logging.INFO and "Audio transcription disabled" in r.getMessage() for r in caplog.records)


def test_the_backends_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        DocumentBackends(parser=PARSER, transcriber=None).parser = object()  # type: ignore[misc]


@pytest.fixture
def card_steps(monkeypatch):
    calls = []

    def record(indexing, container, documents, db_client, env_config, user_repo, quota_service):
        calls.append((indexing, container, documents, db_client, user_repo, quota_service))
        return False

    monkeypatch.setattr(service_container_module, "attach_email_card_step", record)
    return calls


def _container(monkeypatch, *, bucket: str, backends):
    monkeypatch.setenv("APP_ENV", "test")
    env_config = EnvironmentConfig()
    config = {"GEMINI_API_KEY": "dummy-gemini-key", "ANTHROPIC_API_KEY": "dummy-claude-key",
              "CONSOLIDATION": ConsolidationSettings(), "GCS_MEDIA_BUCKET": bucket}
    db = MagicMock(name="firestore_db_client")
    users, quota = MagicMock(name="user_repo"), MagicMock(name="quota_service")
    container = ServiceContainer(config=config, db_client=db, env_config=env_config,
                                 deploy_topology=build_deploy_topology({}, env_config),
                                 account_repo=AsyncMock(spec=AccountRepository), cipher=AesGcmTestCipher(),
                                 user_repo=users, quota_service=quota, document_backends=backends)
    return container, db, users, quota


def test_the_container_builds_the_document_service_over_the_backends_and_passes_it_at_construction(monkeypatch,
                                                                                                  card_steps):
    transcriber = object()
    container, db, users, quota = _container(monkeypatch, bucket="media-bucket",
                                             backends=DocumentBackends(parser=PARSER, transcriber=transcriber))

    conversion = container.file_conversion_service
    documents = conversion._documents
    assert isinstance(documents, DocumentService)
    assert conversion._containers is PARSER
    assert documents._parser is PARSER and documents._transcriber is transcriber and documents._quota is quota
    assert isinstance(documents._transcript_cache, FirestoreTranscriptCache)
    assert documents._reader._context_builder is container.context_builder and documents._reader._user_repo is users
    assert card_steps == [(container.email_indexing_service, container, documents, db, users, quota)]


def test_without_backends_the_conversion_service_has_no_document_service_and_no_container_check(monkeypatch,
                                                                                               card_steps):
    container, *_ = _container(monkeypatch, bucket="media-bucket", backends=None)

    assert container.file_conversion_service._documents is None
    assert container.file_conversion_service._containers is None
    assert [call[2] for call in card_steps] == [None]


def test_without_file_storage_there_is_no_conversion_and_no_document_service(monkeypatch, card_steps):
    container, *_ = _container(monkeypatch, bucket="", backends=DocumentBackends(parser=PARSER, transcriber=None))

    assert container.file_conversion_service is None
    assert [call[2] for call in card_steps] == [None]
