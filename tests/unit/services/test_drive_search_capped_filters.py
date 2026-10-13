"""Filters applied to a capped candidate set say so (USER_DRIVE_INDEXING_RFC §10.1 steps 4-5, §10.2).

A query with a date, folder or kind-family filter ranks the streams' top pages and filters those. When a page came
back full (after the one widening) and the filters dropped candidates, a file that matches may sit past the page:
the answer hedges and never says a flat "No matching files or folders.". A kind family runs one card kNN per member
kind, so the kind filter itself never drops a card candidate."""
from datetime import datetime, timezone

from src.domain.drive_search import render_results
from src.domain.drive_search_texts import ABSENT_IN_SCOPE_LINE, COVERAGE_COMPLETE
from src.services import drive_search_service as search_mod

from .test_drive_search_service import SpyRepo, World

MARCH = dict(date_from=datetime(2026, 3, 1, tzinfo=timezone.utc), date_to=datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc))


def _small_pages(monkeypatch, fetch=3, widened=4):
    monkeypatch.setattr(search_mod, "NAME_FETCH", fetch)
    monkeypatch.setattr(search_mod, "CARD_FETCH", fetch)
    monkeypatch.setattr(search_mod, "FETCH_MAX", widened)


def _query_far_from(w, target_word):
    """The query vector sits on every card except the target's: the card kNN never reaches the target."""
    async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
        w.emb.calls.append(text)
        if task_type == "RETRIEVAL_QUERY":
            return [1.0] + [0.0] * 7
        if target_word in text:
            return [0.0, 1.0] + [0.0] * 6
        return [1.0, 0.01] + [0.0] * 6
    w.emb.get_embedding = emb


class KindSpy(SpyRepo):
    def __init__(self):
        super().__init__()
        self.card_kinds = []

    async def find_nearest_cards(self, user_id, vector, limit, kind=None):
        self.card_kinds.append(kind)
        return await super().find_nearest_cards(user_id, vector, limit, kind)


def _world(**kw):
    w = World(**kw)
    spy = KindSpy()
    w.repo = spy
    w.indexer._repo = spy
    w.search_svc._repo = spy
    return w


async def test_no_hedge_when_the_pages_were_not_full():
    w = World()
    w.drive.add("invoice-1.pdf")
    w.drive.add("invoice-2.pdf")
    await w.settle()
    result = await w.search("invoice", **MARCH)
    text = render_results(result)
    assert result.cards == ()   # v1's `capped` field is gone (P6 review m12); the text below is the hedge
    # §25.13.6 / D29 wording (spec change): complete pages give a flat in-scope negative and no hedge
    assert COVERAGE_COMPLETE in text and ABSENT_IN_SCOPE_LINE.format(word="invoice") in text
    assert "more files may match" not in text


