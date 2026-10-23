"""create_fact and search_existing_facts log shapes (lengths, counts, metadata keys), never fact text, a query or a metadata value."""
import logging
from types import SimpleNamespace

import pytest

from tests.support.fact_store import FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx

_TEXT = "my mother Valentyna Ivanivna lives at Calle Mayor 12"
_META = {
    "account_id": "acc-1", "user_id": "usr-1", "domain": "biographical", "temporal_class": "stable",
    "context_priority": "medium", "tags": ["family-secret-tag"], "metadata": {"name": "Valentyna-Ivanivna-meta"},
    "type": "event", "expiry": EXPIRY_NONE,
}
_SECRETS = ["Valentyna", "Calle Mayor", "family-secret-tag", "Valentyna-Ivanivna-meta", "usr-1", "acc-1"]


async def test_create_fact_logs_no_content_or_metadata_values(caplog):
    store = FactStore()
    with caplog.at_level(logging.DEBUG):
        res = await store.adapter.create_fact(_TEXT, dict(_META), context=ctx())

    assert res["status"] == "created"
    fm = [r.getMessage() for r in caplog.records if "[FactManagement]" in r.getMessage()]
    assert any(f"content_len={len(_TEXT)}" in m and "account_id" in m for m in fm)   # shapes are still there
    for secret in _SECRETS:
        assert not any(secret in m for m in fm), secret


async def _refused_for_its_expiry(store):
    return await store.adapter.create_fact(_TEXT, {k: v for k, v in _META.items() if k != "expiry"}, context=ctx())


async def _store_write_fails(store, monkeypatch):
    async def down(_fact):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(store.repo, "add_fact", down)
    return await store.adapter.create_fact(_TEXT, dict(_META), context=ctx())


# Reviewer edit (facts S3, T4a): create_fact no longer goes through `add_facts_batch` (plan T4a: build_facts, then
# repo.add_fact), so its "saved nothing" branch and its "Failed to create fact" warning are gone; with deduplication
# skipped that branch was reachable only through this monkeypatch. A failed create now ends in one of two branches, a
# refused expiry (WARNING) and a failed write (ERROR); the rule this test pins holds for both: a line that says the create
# failed, and no fact text in any line.
@pytest.mark.parametrize("fail,line", [
    (lambda store, _mp: _refused_for_its_expiry(store), "create_fact refused (expiry)"),
    (_store_write_fails, "Create fact error"),
], ids=["refused_expiry", "write_error"])
async def test_failed_create_fact_warning_logs_no_content(caplog, monkeypatch, fail, line):
    store = FactStore()
    with caplog.at_level(logging.INFO):
        res = await fail(store, monkeypatch)

    assert res["status"] == "failed" and res["fact_id"] is None and store.docs == {}
    fm = [r.getMessage() for r in caplog.records if "[FactManagement]" in r.getMessage()]
    assert any(line in m for m in fm)
    for secret in _SECRETS:
        assert not any(secret in m for m in fm), secret


async def test_search_existing_facts_logs_no_query_keyword_or_fact_text(caplog):
    store = FactStore()
    fact = SimpleNamespace(
        fact_id="fact-12345678", content="Valentyna lives at Calle Mayor 12", relevance_score=0.9, source="text",
        fact_type="event", domain="biographical", temporal_class="stable", state="current",
        context_priority="medium", tags=[], metadata={}, reported_date=None, version=1,
    )
    store.adapter._search_enrichment.enrich_context.return_value = SimpleNamespace(facts=[fact])

    with caplog.at_level(logging.DEBUG):
        results = await store.adapter.search_existing_facts(
            ["secret-keyword", "kw2"], "where does Valentyna live", "alt Calle query", timezone="UTC")

    assert len(results) == 1
    fm = [r.getMessage() for r in caplog.records if "[FactManagement]" in r.getMessage() or r.getMessage().startswith("   [")]
    assert any("keywords=2" in m and "primary_len=" in m for m in fm)     # shapes are still there
    assert any(f"content_len={len(fact.content)}" in m for m in fm)
    for secret in ["secret-keyword", "where does", "Valentyna", "Calle"]:
        assert not any(secret in m for m in fm), secret
