"""The search feeds the term extraction at most QUERY_TERM_CHARS characters of `drive_query`, cut at a word boundary
(USER_DRIVE_INDEXING_RFC §24.6, arbiter R2 S-m1): the query has no length cap and the extraction runs on the event
loop."""
from src.domain.drive_index_params import QUERY_TERM_CHARS
from src.services import drive_search_service as search_mod
from src.services.drive_search_service import DriveSearchService


def test_a_long_query_is_clipped_before_its_terms_are_extracted(monkeypatch):
    seen = []
    monkeypatch.setattr(search_mod, "query_terms", lambda text: seen.append(text) or ())
    DriveSearchService._terms("98765 " * 1000)
    (text,) = seen
    assert len(text) <= QUERY_TERM_CHARS and text.endswith("98765") and text.startswith("98765 98765")


def test_a_term_inside_the_cap_is_still_found():
    assert DriveSearchService._terms("contract 98765 " + "words " * 400) == ("98765",)
