"""Facts S4 · T3a: the kit's `WriteRecorder` forwards the review keywords (since T3c, reviewer edit: of the typed
`apply_fact_update`, the archive being the old argument parsed) to the real adapter over the overlay, so an owner's
"ended" archives the fact beside a history fact in the overlay, gets its report row, and leaves the live store
byte-identical.

Reviewer edit (S4 T8e): `apply_fact_update` returns a typed outcome (T3c review M2); these assertions read its model
dict through the one renderer (`tests.support.fact_write.rendered`), so each keeps its meaning.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import DryRunKit
from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_reasons import answer_reason, history_reason
from src.domain.fact_tool_result import HISTORY_FACT_ID
from src.domain.fact_update import parse_fact_update
from tests.support.fact_kit import ACC, T0, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx, rendered
from tests.unit.scripts.test_s3_dry_run_kit_production_rules import production_adapter

UTC = timezone.utc
ENDED = answer_reason(AnswerKind.ENDED, "q7", "digit")
HISTORY = "No longer true (the owner said so in Oct 2026): works at Acme"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "MEMORY_DIR", tmp_path)
    return tmp_path


async def test_an_owner_archive_with_history_through_the_recorder_lands_in_the_overlay_only(store, memory_dir):
    put_raw(store, "old", "works at Acme", state="stale", stale_since=T0 + timedelta(days=40), reported_date=T0,
            tags=["job", "consolidated"])
    live_before = copy.deepcopy(store.docs)
    said = datetime.now(UTC) - timedelta(hours=1)
    kit = DryRunKit(ledger_path=memory_dir / "spend.json")
    try:
        kit.repo = kit_module.NoWriteFactRepository(store.repo)
        adapter = FactManagementAdapter(kit.repo, store.embedder, store.write, production_adapter(store)._search_enrichment)
        recorder = kit_module.WriteRecorder(adapter, kit)

        result = rendered(await recorder.apply_fact_update(
            "old", parse_fact_update({"state": "archived"}), ACC, context=ctx("owner"), evidence_at=said, reason=ENDED,
            history=HISTORY,
        ))

        assert result["status"] == "updated" and result["fact_id"] == "old", result
        history_id = result[HISTORY_FACT_ID]
        assert store.docs == live_before
        assert [w["kind"] for w in kit.repo.writes] == ["archive_and_add"]
        archived = kit.repo.overlay_document("old")
        assert archived.state is FactState.ARCHIVED and archived.lifecycle_events[-1]["reason"] == ENDED
        new = kit.repo.overlay_document(history_id)
        assert new.text == HISTORY and new.state is FactState.CURRENT and new.expiry_kind is ExpiryKind.NONE
        assert new.reported_date == said and new.lifecycle_events[-1]["reason"] == history_reason("old")
        (row,) = kit.rows
        assert (row["action"], row["actor"], row["status"], row["fact_id"]) == ("update", "owner", "updated", "old")
    finally:
        kit.close()
