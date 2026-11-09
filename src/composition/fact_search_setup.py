"""The fact search service's wiring (fact profile RFC §5): the repository, the embedding service and a place-name index
over the bundled place table, which is read on the first search that names a place, never at build."""
from ..adapters.geo_place_table import load_place_rows
from ..domain.place_names import PlaceNames
from ..ports.embedding_service import EmbeddingService
from ..ports.repository import FactRepository
from ..services.fact_search_service import FactSearchService


def _load_place_names() -> PlaceNames:
    """Reads the table on every call; the service calls it once and keeps the result."""
    return PlaceNames(load_place_rows())


def build_fact_search_service(repository: FactRepository, embedding_service: EmbeddingService) -> FactSearchService:
    return FactSearchService(repository, embedding_service, _load_place_names)
