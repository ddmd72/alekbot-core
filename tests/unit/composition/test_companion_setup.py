"""`build_companions`: one companion memory store and cache under the assembler and the extraction service, one extractor
under the extraction service and the call's text files, each over the container's shared ports."""
import dataclasses
from types import SimpleNamespace

import pytest

from src.composition.companion_setup import build_companions
from src.config.environment import EnvironmentConfig
from src.domain.settings import SearchConfig
from tests.support.firestore_eval import EvalDb

CONTAINER = SimpleNamespace(repository=object(), embedding_service=object(), context_builder=object(),
                            assembly_service=object(), prompt_content_store=object(), user_drive_service=object())
USERS, QUOTA, QUEUE, NOTIFY, SEARCH = object(), object(), object(), object(), object()
LANGUAGES = SimpleNamespace(resolver=object(), localization_port=object())


@pytest.fixture
def companions(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    return build_companions(EvalDb(), EnvironmentConfig(), container=CONTAINER, user_repo=USERS,  # type: ignore[arg-type]
                            quota_service=QUOTA, extraction_queue=QUEUE, notification_service=NOTIFY,
                            languages=LANGUAGES, fact_search=SEARCH)


def test_the_assembler_and_the_extraction_share_one_store_and_cache(companions):
    assembler, extraction = companions.context_assembler, companions.extraction_service

    assert assembler._companion_repo is extraction._companion_repo
    assert assembler._cache_repo is extraction._cache_repo
    assert assembler._embedding is CONTAINER.embedding_service and assembler._fact_repo is CONTAINER.repository
    assert (extraction._queue, extraction._quota, extraction._embedding) == (QUEUE, QUOTA, CONTAINER.embedding_service)
    assert extraction._extractor is companions.extractor_runner


def test_the_assemblers_search_runs_at_the_system_defaults_with_ten_results(companions):
    enrichment, defaults = companions.context_assembler._enrichment, SearchConfig()

    assert enrichment._repo is CONTAINER.repository and enrichment._embedding is CONTAINER.embedding_service
    assert (enrichment._keyword_limit, enrichment._phrase_one_limit, enrichment._phrase_two_limit) == (
        defaults.DEFAULT_KEYWORD_LIMIT, defaults.DEFAULT_PHRASE_ONE_LIMIT, defaults.DEFAULT_PHRASE_TWO_LIMIT)
    assert enrichment._total_limit == 10


def test_the_extractor_runs_over_the_containers_model_setup(companions):
    runner = companions.extractor_runner

    assert runner._context_builder is CONTAINER.context_builder and runner._user_repo is USERS
    assert runner._quota_service is QUOTA and runner._prompt_content_store is CONTAINER.prompt_content_store
    assert runner._language is LANGUAGES.resolver
    assert runner._prompt_builder.repo is None and runner._prompt_builder.assembly_service is CONTAINER.assembly_service


def test_the_calls_text_files_run_the_same_extractor(companions):
    files = companions.call_text_files

    assert files._extractor is companions.extractor_runner
    assert (files._drive, files._notifications, files._users) == (CONTAINER.user_drive_service, NOTIFY, USERS)
    assert files._localization is LANGUAGES.localization_port and files._language is LANGUAGES.resolver
    assert files._prompt_content is CONTAINER.prompt_content_store


def test_the_bundle_is_frozen(companions):
    with pytest.raises(dataclasses.FrozenInstanceError):
        companions.extractor_runner = None  # type: ignore[misc]
