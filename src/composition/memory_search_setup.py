"""The one fact search (FACT_PROFILE_RFC §5) and what it shares with the profile step: the people cards (RFC §7) and the
one place atlas, which `ApplicationGraph.start()` loads before the instance serves, so the first search does not pay for it."""
from dataclasses import dataclass
from typing import Any

from ..adapters.firestore_person_card_repo import FirestorePersonCardRepository
from ..config.environment import EnvironmentConfig
from ..services.fact_search_service import FactSearchService
from .fact_profile_setup import LazyAtlas
from .fact_search_setup import build_fact_search_service


@dataclass(frozen=True)
class MemorySearch:
    person_cards: FirestorePersonCardRepository
    place_atlas: LazyAtlas
    fact_search: FactSearchService


def build_memory_search(db_client: Any, env_config: EnvironmentConfig, *, container: Any) -> MemorySearch:
    """The search over the container's fact repository and embedding adapter; the agent factory hands it to the memory
    agent of every user."""
    person_cards = FirestorePersonCardRepository(db_client, env_config)
    atlas = LazyAtlas()
    return MemorySearch(
        person_cards=person_cards,
        place_atlas=atlas,
        fact_search=build_fact_search_service(container.repository, container.embedding_service, person_cards,
                                              atlas=atlas),
    )
