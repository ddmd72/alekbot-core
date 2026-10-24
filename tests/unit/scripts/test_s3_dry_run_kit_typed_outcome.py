"""The kit's `WriteRecorder` reads a typed update's `FactUpdateOutcome` (facts S4 · T8e, T3c review M2): the report row
of a typed write is filled from the outcome, in the words of the model's dict, and the outcome is returned unchanged."""
from datetime import timedelta

import pytest

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import DryRunKit
from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.entities import FactState
from src.domain.fact_update import ParsedFactUpdate, parse_fact_update
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from tests.support.fact_kit import ACC, T0, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx, horizon
from tests.unit.scripts.test_s3_dry_run_kit_production_rules import production_adapter


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "MEMORY_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def kit(memory_dir):
    kit = DryRunKit(ledger_path=memory_dir / "spend.json")
    yield kit
    kit.close()


def recorder_over(store, kit):
    kit.repo = kit_module.NoWriteFactRepository(store.repo)
    adapter = FactManagementAdapter(kit.repo, store.embedder, store.write, production_adapter(store)._search_enrichment)
    return kit_module.WriteRecorder(adapter, kit)


def seeded():
    store = FactStore(indexes=INDEXES_FILE)
    put_raw(store, "cur", "works at Acme", state="current", reported_date=T0, expiry_kind="horizon", ttl_days=30,
            expires_at=T0 + timedelta(days=400), tags=["job", "consolidated"])
    return store


async def test_a_written_typed_update_gets_its_row_from_the_outcome(kit):
    recorder = recorder_over(seeded(), kit)

    outcome = await recorder.apply_fact_update(
        "cur", parse_fact_update({"state": "invalidated", "expiry": horizon(3)}), ACC, context=ctx("stage1"),
    )

    assert isinstance(outcome, FactUpdateOutcome) and outcome.written
    (row,) = kit.rows
    assert (row["status"], row["refused"], row["fact_id"], row["state_stored"]) == ("updated", None, "cur", "invalidated")
    assert row["notes"] == "expiry is not used when a fact is closed; ignored" and row["derived_from_unreadable"] is False


async def test_a_refused_typed_update_gets_the_refusals_words(kit):
    recorder = recorder_over(seeded(), kit)

    outcome = await recorder.apply_fact_update(
        "cur", parse_fact_update({"state": "current", "expiry": horizon(30)}), ACC, context=ctx("stage1"),
    )

    assert outcome.refusal is UpdateRefusal.BORN_DUE
    (row,) = kit.rows
    assert (row["status"], row["refused"], row["fact_id"], row["decision_stored"]) == ("failed", "expiry", "cur", None)
    assert row["notes"] is None


async def test_a_missing_fact_is_a_not_found_row(kit):
    recorder = recorder_over(seeded(), kit)

    outcome = await recorder.apply_fact_update("missing", ParsedFactUpdate(state=FactState.ARCHIVED), ACC,
                                               context=ctx("stage1"))

    assert outcome.refusal is UpdateRefusal.NOT_FOUND
    (row,) = kit.rows
    assert (row["status"], row["fact_id"], row["state_stored"]) == ("not_found", "missing", None)
