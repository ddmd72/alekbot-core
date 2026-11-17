"""`build_memory_search`: the fact search over the container's repository and embedding adapter reads the bundle's people
cards and the bundle's one place atlas (never a load of its own)."""
import dataclasses
from types import SimpleNamespace

import pytest

from src.composition.fact_profile_setup import LazyAtlas
from src.composition.memory_search_setup import build_memory_search
from src.config.environment import EnvironmentConfig
from tests.support.firestore_eval import EvalDb

CONTAINER = SimpleNamespace(repository=object(), embedding_service=object())


@pytest.fixture
def search(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    return build_memory_search(EvalDb(), EnvironmentConfig(), container=CONTAINER)


def test_the_search_reads_the_bundles_cards_and_atlas(search):
    service = search.fact_search

    assert service._repository is CONTAINER.repository and service._embedder is CONTAINER.embedding_service
    assert service._person_cards is search.person_cards
    assert isinstance(search.place_atlas, LazyAtlas)
    assert service._place_loader == search.place_atlas.names


def test_the_bundle_is_frozen(search):
    with pytest.raises(dataclasses.FrozenInstanceError):
        search.fact_search = None  # type: ignore[misc]
