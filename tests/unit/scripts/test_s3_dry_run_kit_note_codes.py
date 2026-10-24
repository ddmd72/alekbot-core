"""M3: the kit counts an unreadable `derived_from` through the result's note code, not by matching prose."""
import pytest

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import DryRunKit
from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.fact_tool_result import NOTE_NO_READABLE_SOURCE
from src.domain.fact_write_context import NO_READABLE_SOURCE_NOTE
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx
from tests.unit.scripts.test_s3_dry_run_kit import meta, seed
from tests.unit.scripts.test_s3_dry_run_kit_production_rules import production_adapter


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "MEMORY_DIR", tmp_path)
    return tmp_path


async def test_a_cleanup_create_with_an_unreadable_derived_from_is_counted_through_the_code(store, memory_dir):
    kit = DryRunKit(ledger_path=memory_dir / "spend.json")
    try:
        kit.repo = kit_module.NoWriteFactRepository(store.repo)
        adapter = FactManagementAdapter(kit.repo, store.embedder, store.write, production_adapter(store)._search_enrichment)
        recorder = kit_module.WriteRecorder(adapter, kit)

        result = await recorder.create_fact("walks daily", meta(derived_from=["no-such-fact"]),
                                            context=ctx(actor="stage2a", timezone="UTC"))

        assert result["note_codes"] == [NOTE_NO_READABLE_SOURCE]
        assert kit.rows[0]["derived_from_unreadable"] is True
        assert kit.report()["cleanup_creates_without_derived_from"] == {"per_pass": {"stage2a": 1}, "total": 1}
    finally:
        kit.close()


async def test_an_unreadable_derived_from_beside_a_kept_reported_date_is_still_counted(store, memory_dir):
    """The model also sent a `reported_date`: two notes reach the result, each whole, so the code is still found."""
    kit = DryRunKit(ledger_path=memory_dir / "spend.json")
    try:
        kit.repo = kit_module.NoWriteFactRepository(store.repo)
        adapter = FactManagementAdapter(kit.repo, store.embedder, store.write, production_adapter(store)._search_enrichment)
        recorder = kit_module.WriteRecorder(adapter, kit)

        result = await recorder.create_fact(
            "walks daily", meta(derived_from=["no-such-fact"], reported_date="2026-10-20T08:00:00+00:00"),
            context=ctx(actor="stage2a", timezone="UTC"),
        )

        assert result["note"].startswith(NO_READABLE_SOURCE_NOTE + "; reported_date is kept by the system")
        assert result["note_codes"] == [NOTE_NO_READABLE_SOURCE]
        assert kit.rows[0]["derived_from_unreadable"] is True
        assert kit.report()["cleanup_creates_without_derived_from"] == {"per_pass": {"stage2a": 1}, "total": 1}
    finally:
        kit.close()


async def test_a_note_with_the_same_words_but_no_code_is_not_counted(store, memory_dir):
    kit = DryRunKit(ledger_path=memory_dir / "spend.json")
    try:
        kit.repo = kit_module.NoWriteFactRepository(store.repo)
        adapter = FactManagementAdapter(kit.repo, store.embedder, store.write, production_adapter(store)._search_enrichment)
        recorder = kit_module.WriteRecorder(
            _Uncoded(adapter), kit)

        await recorder.create_fact("walks daily", meta(derived_from=["no-such-fact"]),
                                   context=ctx(actor="stage2a", timezone="UTC"))

        assert "no readable fact" in kit.rows[0]["notes"] and kit.rows[0]["derived_from_unreadable"] is False
    finally:
        kit.close()


class _Uncoded:
    """The adapter, with `note_codes` stripped from its results."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def create_fact(self, *args, **kwargs):
        result = await self._real.create_fact(*args, **kwargs)
        result.pop("note_codes", None)
        return result
