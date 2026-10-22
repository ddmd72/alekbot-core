"""create_fact logs shapes (content length, metadata keys), never the fact text or a metadata value."""
import logging

from tests.support.fact_store import FactStore

_TEXT = "my mother Maria Doe lives at 12 Oak Street"
_META = {
    "account_id": "acc-1", "user_id": "usr-1", "domain": "biographical", "temporal_class": "stable",
    "context_priority": "medium", "tags": ["family-secret-tag"], "metadata": {"name": "Maria-Doe-meta"},
    "type": "event",
}
_SECRETS = ["Maria", "Oak Street", "family-secret-tag", "Maria-Doe-meta", "usr-1", "acc-1"]


async def test_create_fact_logs_no_content_or_metadata_values(caplog):
    store = FactStore()
    with caplog.at_level(logging.DEBUG):
        res = await store.adapter.create_fact(_TEXT, dict(_META))

    assert res["status"] == "created"
    fm = [r.getMessage() for r in caplog.records if "[FactManagement]" in r.getMessage()]
    assert any(f"content_len={len(_TEXT)}" in m and "account_id" in m for m in fm)   # shapes are still there
    for secret in _SECRETS:
        assert not any(secret in m for m in fm), secret


async def test_failed_create_fact_warning_logs_no_content(caplog, monkeypatch):
    store = FactStore()

    async def not_saved(**_kw):
        return False, 0, []

    monkeypatch.setattr(store.write, "add_facts_batch", not_saved)
    with caplog.at_level(logging.INFO):
        res = await store.adapter.create_fact(_TEXT, dict(_META))

    assert res["status"] == "failed"
    fm = [r.getMessage() for r in caplog.records if "[FactManagement]" in r.getMessage()]
    assert any("Failed to create fact" in m for m in fm)
    assert not any("Valentyna" in m or "Calle Mayor" in m for m in fm)
