"""The stored expiry decision through both enrichment mappers (Facts S3 · T3): vector search and the domain channel carry
`expiry_kind`, `ttl_days` and `expires_at` onto `EnrichedFact`, which reads them back to the model's view in the
owner's calendar. A real repository on the evaluating Firestore fake; the embedder is the deterministic hash one.

Reviewer edit (Facts S4 T6c, L4 and M6): `expires_at` is an aware UTC datetime (no longer an ISO string), the view is
`consolidation_hit` (no longer `EnrichedFact.expiry_decision`), and the context crosses a Cloud Task through the task queue's
one encoder, `_DomainEncoder`, which writes datetimes and enums (no longer through a plain `json.dumps`).
"""
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.gcp_task_queue import _DomainEncoder
from src.domain.entities import ExpiryKind
from src.domain.request_context import RequestContext
from src.domain.search import consolidation_hit
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, T0, USER, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore

LISBON = "Europe/Lisbon"
# 2026-07-10 23:00 UTC is the end of 2026-07-10 in Europe/Lisbon (WEST).
DATE_DUE = datetime(2026, 7, 10, 23, 0, tzinfo=timezone.utc)
HORIZON_DUE = T0 + timedelta(days=30)


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture
def service(store):
    return SearchEnrichmentService(store.repo, store.embedder, keyword_limit=10, phrase_one_limit=10,
                                   phrase_two_limit=10, total_limit=30)


async def seed_decisions(store):
    await store.repo.add_fact(make_fact("dated", expiry_kind=ExpiryKind.DATE, expires_at=DATE_DUE))
    await store.repo.add_fact(make_fact("horizon", expiry_kind=ExpiryKind.HORIZON, ttl_days=30, expires_at=HORIZON_DUE))
    await store.repo.add_fact(make_fact("permanent", expiry_kind=ExpiryKind.NONE))
    await store.repo.add_fact(make_fact("undecided"))


async def enrich(service, **kw):
    with RequestContext(user_id=USER, account_id=ACC):
        context = await service.enrich_context(
            ["k"], "phrase one", "phrase two", dedup_threshold=1.0, skip_semantic_dedup=True, **kw)
    return {f.content: f for f in context.facts}


def decision(fact, zone):
    view = consolidation_hit(fact, zone)  # T6c re-review, S3: a hit's view is consolidation_hit
    return view["expiry"], view["due"]


async def test_vector_search_carries_the_decision_and_the_view_reads_it_back(store, service):
    await seed_decisions(store)
    facts = await enrich(service)
    assert {k: (f.expiry_kind, f.ttl_days) for k, f in facts.items()} == {
        "dated": (ExpiryKind.DATE, None), "horizon": (ExpiryKind.HORIZON, 30),
        "permanent": (ExpiryKind.NONE, None), "undecided": (None, None)}
    assert decision(facts["dated"], LISBON) == ({"kind": "date", "date": "2026-07-10"}, None)
    assert decision(facts["horizon"], LISBON) == ({"kind": "horizon", "days": 30}, "2026-01-31")
    assert decision(facts["permanent"], LISBON) == ({"kind": "none"}, None)
    assert decision(facts["undecided"], LISBON) == (None, None)


async def test_the_domain_channel_alone_carries_the_decision_too(store, service):
    await seed_decisions(store)
    with RequestContext(user_id=USER, account_id=ACC):
        context = await service.enrich_context([], "", "", relevant_domains=["health"], skip_semantic_dedup=True)
    facts = {f.content: f for f in context.facts}
    assert {f.source for f in facts.values()} == {"domain_direct"}
    assert facts["dated"].expiry_kind is ExpiryKind.DATE and facts["dated"].expires_at == DATE_DUE
    assert decision(facts["horizon"], LISBON) == ({"kind": "horizon", "days": 30}, "2026-01-31")


async def test_expires_at_is_an_aware_utc_datetime(store, service):
    await seed_decisions(store)
    facts = await enrich(service)
    assert facts["dated"].expires_at == DATE_DUE and facts["dated"].expires_at.utcoffset() == timedelta(0)
    assert facts["permanent"].expires_at is None and facts["undecided"].expires_at is None


async def test_the_enriched_context_still_serialises_to_json_for_a_cloud_task(store, service):
    await seed_decisions(store)
    facts = await enrich(service)
    payload = json.loads(json.dumps(asdict(facts["horizon"]), cls=_DomainEncoder))
    assert payload["expiry_kind"] == "horizon" and payload["ttl_days"] == 30
    assert payload["expires_at"] == HORIZON_DUE.isoformat()


async def test_a_naive_stored_expires_at_is_read_as_utc(store, service):
    await store.repo.add_fact(make_fact("naive", expiry_kind=ExpiryKind.DATE, expires_at=datetime(2026, 7, 10, 23, 0)))
    facts = await enrich(service)
    assert facts["naive"].expires_at == datetime(2026, 7, 10, 23, 0, tzinfo=timezone.utc)
    assert decision(facts["naive"], LISBON)[0] == {"kind": "date", "date": "2026-07-10"}
