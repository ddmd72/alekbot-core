"""Facts S3 · T12b review R3: the own-content search of a written fact returns at most `limit` hits, as production's does."""
from typing import Any, Dict, List

from scripts.consolidation.s3_dry_run_kit import OwnFactsSearch


class FixedSearch:
    def __init__(self, hits: List[Dict[str, Any]]) -> None:
        self.hits = hits
        self.asked: List[int] = []

    async def search_existing_facts(self, keywords, primary_query, alternative_query="", limit=20, *, timezone):
        self.asked.append(limit)
        return self.hits[:limit]


def hits(n: int) -> List[Dict[str, Any]]:
    return [{"fact_id": f"live{i}"} for i in range(n)]


async def test_the_own_fact_takes_one_of_the_limit_places_and_the_last_live_hit_drops_out():
    own = {"walks daily": [{"fact_id": "own"}]}
    search = OwnFactsSearch(FixedSearch(hits(20)), own)

    result = await search.search_existing_facts(["walks"], "walks daily", limit=20, timezone="UTC")

    assert len(result) == 20
    assert [h["fact_id"] for h in result[:2]] == ["own", "live0"] and result[-1]["fact_id"] == "live18"


async def test_the_limit_is_forwarded_and_a_short_result_is_not_padded():
    inner = FixedSearch(hits(3))
    search = OwnFactsSearch(inner, {"walks daily": [{"fact_id": "own"}]})

    result = await search.search_existing_facts(["walks"], "walks daily", limit=5, timezone="UTC")

    assert inner.asked == [5] and [h["fact_id"] for h in result] == ["own", "live0", "live1", "live2"]


async def test_a_query_with_no_own_fact_returns_the_live_hits_only():
    search = OwnFactsSearch(FixedSearch(hits(20)), {"other": [{"fact_id": "own"}]})

    result = await search.search_existing_facts(["walks"], "walks daily", limit=20, timezone="UTC")

    assert [h["fact_id"] for h in result] == [f"live{i}" for i in range(20)]
