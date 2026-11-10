"""The fact search service's wiring (fact profile RFC §5): the repository, the embedding service, the people-card store
and a place-name index over the bundled place table, which is shared with the profile step (`LazyAtlas`) and loaded at
service start; a failed load is retried by the first search that names a place or carries the owner's home."""
from typing import Optional

from ..adapters.geo_place_table import load_place_rows
from .fact_profile_setup import LazyAtlas
from ..domain.place_names import PlaceNames
from ..ports.embedding_service import EmbeddingService
from ..ports.person_card_repository import PersonCardRepository
from ..ports.repository import FactRepository
from ..services.fact_search_service import FactSearchService


def _load_place_names() -> PlaceNames:
    """Reads the table on every call; the service calls it once and keeps the result."""
    return PlaceNames(load_place_rows())


def build_fact_search_service(
    repository: FactRepository, embedding_service: EmbeddingService, person_cards: PersonCardRepository,
    atlas: Optional[LazyAtlas] = None,
) -> FactSearchService:
    """With `atlas` the search reads the process's one place table (loaded at boot); without it, its own load."""
    return FactSearchService(repository, embedding_service, atlas.names if atlas else _load_place_names, person_cards)
