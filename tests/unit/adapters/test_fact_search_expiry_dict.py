"""What the consolidator sees of a stored expiry decision (Facts S3, T5, plan §3.3).

`search_existing_facts` renders the decision as `expiry` in the shape the model sends, plus `due` for a horizon, and
`reported_date` with the owner's offset; `ttl_days` / `expires_at` never reach the model. Real adapter, enrichment service
and repository on the evaluating Firestore fake (index refusal ON); the writes go through the adapter, so what is shown is
what the code stored.
"""
import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.request_context import RequestContext
from src.domain.search import EnrichedContext, EnrichedFact
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon, on

PARIS = ZoneInfo("Europe/Paris")
TOKYO = "Asia/Tokyo"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def meta(**over):
    base = {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family"], "metadata": {"name": "Maria"}, "type": "event",
    }
    base.update(over)
    return base


async def create(store, text="mother is Maria", **over):
    res = await store.adapter.create_fact(text, meta(**over), context=ctx())
    assert res["status"] == "created", res
    return res["fact_id"]


async def search(store, zone="Europe/Paris"):
    adapter = FactManagementAdapter(store.repo, store.embedder, store.write,
                                    SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20))
    with RequestContext(user_id=USER, account_id=ACC):
        return await adapter.search_existing_facts(["family"], "mother is Maria", timezone=zone)


def in_days(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).astimezone(PARIS).date()


async def test_a_date_decision_is_shown_as_the_last_day_it_holds_in_the_owners_calendar(store):
    day = in_days(30)
    await create(store, expiry=on(day))
    (got,) = await search(store)
    assert got["expiry"] == {"kind": "date", "date": day.isoformat()} and got["due"] is None


async def test_a_date_view_sent_back_unchanged_keeps_the_stored_moment(store):
    fid = await create(store, expiry=on(in_days(30)))
    stored = store.docs[fid]["expires_at"]
    (got,) = await search(store)
    res = await store.adapter.update_fact(fid, {"expiry": got["expiry"]}, ACC, context=ctx())
    assert res["status"] == "updated"
    assert store.docs[fid]["expires_at"] == stored


async def test_a_horizon_is_shown_with_its_days_and_the_local_day_it_falls_due(store):
    reported = datetime.now(timezone.utc) - timedelta(days=10)
    fid = await create(store, expiry=horizon(90), reported_date=reported.isoformat())
    (got,) = await search(store)
    due = store.docs[fid]["expires_at"].astimezone(PARIS).date().isoformat()
    assert got["expiry"] == {"kind": "horizon", "days": 90} and got["due"] == due


async def test_none_is_shown_as_none_without_a_due_day(store):
    await create(store, expiry=EXPIRY_NONE)
    (got,) = await search(store)
    assert got["expiry"] == EXPIRY_NONE and got["due"] is None


async def test_a_fact_with_no_recorded_kind_shows_no_decision_whatever_ttl_days_holds(store):
    put_raw(store, "legacy", "mother is Maria", entity={"domain": "biographical", "ttl_days": 90},
            expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc))
    (got,) = await search(store)
    assert got["expiry"] is None and got["due"] is None


async def test_the_model_never_sees_ttl_days_or_expires_at(store):
    await create(store, expiry=horizon(90))
    (got,) = await search(store)
    assert "ttl_days" not in got and "expires_at" not in got and "expiry_kind" not in got
    assert set(got) >= {"expiry", "due", "reported_date"}
    json.dumps(got)


async def test_reported_date_carries_the_owners_offset(store):
    await create(store, expiry=EXPIRY_NONE, reported_date="2026-10-22T10:00:00+00:00")
    (paris,) = await search(store, "Europe/Paris")
    (tokyo,) = await search(store, TOKYO)
    (utc,) = await search(store, "UTC")
    assert paris["reported_date"] == "2026-10-22T12:00:00+02:00"
    assert tokyo["reported_date"] == "2026-10-22T19:00:00+09:00"
    assert utc["reported_date"] == "2026-10-22T10:00:00+00:00"


async def test_an_unknown_zone_is_read_as_utc(store):
    await create(store, expiry=EXPIRY_NONE, reported_date="2026-10-22T10:00:00+00:00")
    (got,) = await search(store, "Not/AZone")
    assert got["reported_date"] == "2026-10-22T10:00:00+00:00"


class _Enrichment:
    def __init__(self, fact):
        self._fact = fact

    async def enrich_context(self, **_):
        return EnrichedContext(facts=[self._fact], total_sources=1, dedup_count=0, biographical_dedup_count=0)


async def test_a_reported_date_that_is_not_iso_is_shown_as_stored_and_the_rest_still_renders():
    fact = EnrichedFact(fact_id="f", content="c", source="keyword", reported_date="last spring")
    adapter = FactManagementAdapter(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), _Enrichment(fact))
    (got,) = await adapter.search_existing_facts(["k"], "q", timezone="Europe/Paris")
    assert got["reported_date"] == "last spring" and got["expiry"] is None


async def test_a_missing_reported_date_stays_missing():
    fact = EnrichedFact(fact_id="f", content="c", source="keyword")
    adapter = FactManagementAdapter(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), _Enrichment(fact))
    (got,) = await adapter.search_existing_facts(["k"], "q", timezone="Europe/Paris")
    assert got["reported_date"] is None


async def test_the_date_shown_follows_the_zone_it_is_asked_in():
    """The same moment is a different calendar day east of Paris: the owner's zone, not the host's, decides."""
    midnight_paris = datetime.combine(datetime(2026, 12, 25).date() + timedelta(days=1), time.min, tzinfo=PARIS)
    fact = EnrichedFact(fact_id="f", content="c", source="keyword", expiry_kind="date",
                        expires_at=midnight_paris.astimezone(timezone.utc).isoformat())
    adapter = FactManagementAdapter(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), _Enrichment(fact))
    (paris,) = await adapter.search_existing_facts(["k"], "q", timezone="Europe/Paris")
    (tokyo,) = await adapter.search_existing_facts(["k"], "q", timezone=TOKYO)
    assert paris["expiry"] == {"kind": "date", "date": "2026-12-25"}
    assert tokyo["expiry"] == {"kind": "date", "date": "2026-12-26"}
