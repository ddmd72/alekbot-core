"""Code's typed update (facts S4 · T3c, inward review L1): the review and the owner write through
`FactUpdatePort.apply_fact_update` with a `ParsedFactUpdate` and the inputs only code has (`evidence_at`, `reason`,
`history`); the model's `update_fact` carries none of them. The consolidation tracker and the dry-run kit's recorder
forward the typed path and record its writes.

On the evaluating Firestore fake with the index refusal ON, through the real repository, write service and adapter.
"""
import copy
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import DryRunKit
from src.adapters.fact_management_adapter import FactManagementAdapter
from src.agents.consolidation_agent import _TrackingFactManagement
from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_expiry import Expiry
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import ARCHIVE_ONLY, answer_updates
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import answer_reason, basis_reason, history_reason
from src.domain.fact_tool_result import HISTORY_FACT_ID
from src.domain.fact_update import ParsedFactUpdate
from src.ports.fact_management_port import FactManagementPort
from tests.support.fact_kit import ACC, T0, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx
from tests.support.review_rule_builders import question

UTC = timezone.utc
ACTORS = ["review", "owner"]
STALE_SINCE = T0 + timedelta(days=40)
ENDED = answer_reason(AnswerKind.ENDED, "q7", "digit")
REWRITTEN = basis_reason("rewrite", ReviewBasis("reply", question_id="q7", quote="moved to Lisbon"))
HISTORY = "No longer true (the owner said so in Oct 2026): works at Acme"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def seed(store, doc_id="old", text="works at Acme"):
    return put_raw(
        store, doc_id, text, state="stale", stale_since=STALE_SINCE, is_current=True, reported_date=T0,
        expiry_kind="horizon", ttl_days=30, expires_at=T0 + timedelta(days=30), tags=["work", "consolidated"],
        lifecycle_events=[],
    )


def last_entry(store, doc_id):
    return store.docs[doc_id]["lifecycle_events"][-1]


# ------------------------------------------------------------------------------------------------ the two entries

def test_the_models_update_carries_no_code_only_input():
    params = list(inspect.signature(FactManagementPort.update_fact).parameters)
    assert params == ["self", "fact_id", "updates", "account_id", "context"]


async def test_a_review_write_through_the_models_entry_has_no_reason_and_is_refused(store):
    seed(store)
    before = copy.deepcopy(store.docs)

    res = await store.adapter.update_fact("old", {"state": "archived"}, ACC, context=ctx("review"))

    assert res["status"] == "failed" and "code-built trail reason" in res["message"], res
    assert store.docs == before


# ------------------------------------------------------------------------------------------------ the owner's answers

async def test_still_true_from_answer_updates_revives_the_stale_fact(store):
    seed(store)
    stored = await store.repo.get_fact_by_id("old")
    said = datetime.now(UTC) - timedelta(hours=1)
    change = answer_updates(AnswerKind.STILL_TRUE, stored, question(fact_id="old"), said, "Europe/Madrid", "digit")

    res = await store.adapter.apply_fact_update(
        "old", change.update, ACC, context=ctx("owner"), evidence_at=change.evidence_at, reason=change.reason,
    )

    assert res["status"] == "updated" and res["fact_id"] == "old" and HISTORY_FACT_ID not in res, res
    doc = store.docs["old"]
    assert doc["state"] == "current" and doc["stale_since"] is None and doc["reported_date"] == said
    assert doc["expiry_kind"] == "horizon" and doc["expires_at"] > datetime.now(UTC)
    entry = last_entry(store, "old")
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == ("stale", "current", "owner", change.reason)


@pytest.mark.parametrize("actor", ACTORS)
async def test_an_archive_with_history_writes_the_archive_and_the_history_fact(store, actor):
    seed(store)
    said = datetime.now(UTC) - timedelta(hours=2)
    before = set(store.docs)

    res = await store.adapter.apply_fact_update(
        "old", ARCHIVE_ONLY, ACC, context=ctx(actor), evidence_at=said, reason=ENDED, history=HISTORY,
    )

    assert res["status"] == "updated" and res["fact_id"] == "old", res
    history_id = res[HISTORY_FACT_ID]
    assert set(store.docs) == before | {history_id}
    old = store.docs["old"]
    assert old["state"] == "archived" and old["stale_since"] == STALE_SINCE
    entry = last_entry(store, "old")
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == ("stale", "archived", actor, ENDED)
    new = store.docs[history_id]
    assert new["text"] == HISTORY and new["state"] == "current" and new["reported_date"] == said
    assert new["expiry_kind"] == "none" and new["lifecycle_events"][-1]["reason"] == history_reason("old")


@pytest.mark.parametrize("actor", ACTORS)
async def test_a_typed_rewrite_is_a_current_version(store, actor):
    seed(store)

    res = await store.adapter.apply_fact_update(
        "old", ParsedFactUpdate(content="works at Beta", expiry=Expiry(ExpiryKind.HORIZON, days=90)), ACC,
        context=ctx(actor), reason=REWRITTEN,
    )

    assert res["status"] == "updated" and res["fact_id"] != "old" and res["version"] == 2, res
    new = store.docs[res["fact_id"]]
    assert new["text"] == "works at Beta" and new["state"] == "current"
    assert new["lifecycle_events"][-1]["reason"] == REWRITTEN
    assert store.docs["old"]["superseded_by"] == res["fact_id"]


# ------------------------------------------------------------------------------------------------ refusals

async def test_history_with_anything_but_the_archive_alone_is_failed_with_the_domains_reason(store):
    seed(store)
    before = copy.deepcopy(store.docs)

    res = await store.adapter.apply_fact_update(
        "old", ParsedFactUpdate(state=FactState.INVALIDATED), ACC, context=ctx("owner"), evidence_at=datetime.now(UTC),
        reason=ENDED, history=HISTORY,
    )

    assert res == {
        "fact_id": "old", "status": "failed",
        "message": "history goes only with an update that archives the fact and changes nothing else; nothing was written",
    }
    assert store.docs == before


async def test_a_review_write_without_its_reason_is_failed_with_the_domains_reason(store):
    seed(store)
    before = copy.deepcopy(store.docs)

    res = await store.adapter.apply_fact_update("old", ARCHIVE_ONLY, ACC, context=ctx("review"))

    assert res["status"] == "failed" and "a review write carries its code-built trail reason" in res["message"], res
    assert store.docs == before


async def test_a_missing_fact_is_not_found(store):
    res = await store.adapter.apply_fact_update("nope", ARCHIVE_ONLY, ACC, context=ctx("owner"), reason=ENDED)
    assert res == {"fact_id": "nope", "status": "not_found", "message": "Fact not found"}


# ------------------------------------------------------------------------------------------------ the wrappers

async def test_the_tracker_forwards_the_typed_update_and_records_the_history_fact(store):
    seed(store)
    tracker = _TrackingFactManagement(store.adapter)

    res = await tracker.apply_fact_update(
        "old", ARCHIVE_ONLY, ACC, context=ctx("owner"), evidence_at=datetime.now(UTC), reason=ENDED, history=HISTORY,
    )

    assert res["status"] == "updated" and store.docs["old"]["state"] == "archived"
    assert tracker.wrote is True and tracker.changed == [(res[HISTORY_FACT_ID], HISTORY)]


async def test_the_tracker_records_a_typed_rewrite_under_the_version_written(store):
    seed(store)
    tracker = _TrackingFactManagement(store.adapter)

    res = await tracker.apply_fact_update(
        "old", ParsedFactUpdate(content="works at Beta", expiry=Expiry(ExpiryKind.HORIZON, days=90)), ACC,
        context=ctx("review"), reason=REWRITTEN,
    )

    assert tracker.changed == [(res["fact_id"], "works at Beta")] and res["fact_id"] != "old"


async def test_the_tracker_records_nothing_for_a_refused_typed_update(store):
    seed(store)
    tracker = _TrackingFactManagement(store.adapter)

    await tracker.apply_fact_update("old", ARCHIVE_ONLY, ACC, context=ctx("review"))

    assert tracker.wrote is False and tracker.changed == []


class _ModelOnly(FactManagementPort):
    async def search_existing_facts(self, keywords, primary_query, alternative_query="", limit=20, *, timezone):
        return []

    async def create_fact(self, content, metadata, *, context):
        return {}

    async def update_fact(self, fact_id, updates, account_id, *, context):
        return {}

    async def merge_facts(self, fact_ids, merged_content, metadata, *, context):
        return {}


async def test_the_tracker_over_a_port_without_the_typed_path_says_so():
    with pytest.raises(TypeError, match="FactUpdatePort"):
        await _TrackingFactManagement(_ModelOnly()).apply_fact_update("f", ARCHIVE_ONLY, ACC, context=ctx("owner"))


async def test_the_kits_recorder_forwards_the_typed_update_into_the_overlay_only(store, tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "MEMORY_DIR", tmp_path)
    seed(store)
    live_before = copy.deepcopy(store.docs)
    kit = DryRunKit(ledger_path=tmp_path / "spend.json")
    try:
        kit.repo = kit_module.NoWriteFactRepository(store.repo)
        adapter = FactManagementAdapter(kit.repo, store.embedder, store.write, store.adapter._search_enrichment)
        recorder = kit_module.WriteRecorder(adapter, kit)

        res = await recorder.apply_fact_update(
            "old", ARCHIVE_ONLY, ACC, context=ctx("owner"), evidence_at=datetime.now(UTC), reason=ENDED, history=HISTORY,
        )

        assert res["status"] == "updated", res
        assert store.docs == live_before
        assert [w["kind"] for w in kit.repo.writes] == ["archive_and_add"]
        assert kit.repo.overlay_document("old").state is FactState.ARCHIVED
        assert kit.repo.overlay_document(res[HISTORY_FACT_ID]).text == HISTORY
        (row,) = kit.rows
        assert (row["action"], row["actor"], row["status"]) == ("update", "owner", "updated")
    finally:
        kit.close()
