"""scripts/data_audit/facts.py: each fact invariant on raw stored documents, one violating document per class."""
import copy
from datetime import datetime, timedelta, timezone

import pytest
from google.cloud.firestore_v1.vector import Vector

from scripts.data_audit.facts import check_fact, check_lineages
from src.domain.entities import FactState
from src.domain.fact_lifecycle import _LIFECYCLE
from src.domain.fact_lifecycle_sweep import stale_fields
from tests.support.fact_store import FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon

ACC = "acc-1"
NOW = datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
STATES = [s.value for s in FactState]


def raw(**over):
    """A stored fact as a plain dict (not built through FactEntity)."""
    doc = {
        "id": "f1", "account_id": ACC, "created_by_user_id": "usr-1", "lineage_id": "L1",
        "text": "mother is Maria", "type": "event", "state": "current", "is_current": True, "valid_to": None,
        "vector": Vector([0.1, 0.2]), "tags_vector": Vector([0.3]), "metadata_vector": Vector([0.4]),
        "created_at": NOW, "reported_date": NOW, "valid_from": NOW, "last_updated": NOW,
    }
    doc.update(over)
    return doc


def without(*keys, **over):
    doc = raw(**over)
    for k in keys:
        doc.pop(k, None)
    return doc


def test_a_clean_document_has_no_violation():
    assert check_fact("f1", raw()) == []


@pytest.mark.parametrize("state", STATES)
def test_each_state_with_its_lifecycle_flags_is_clean(state):
    is_current, closes = _LIFECYCLE[FactState(state)]
    doc = raw(state=state, is_current=is_current, valid_to=NOW if closes else None)
    assert check_fact("f1", doc) == []


async def _created(store, expiry):
    res = await store.adapter.create_fact("mother is Maria", {
        "account_id": ACC, "user_id": "usr-1", "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family"], "metadata": {"name": "Maria"}, "type": "event",
        "expiry": expiry,
    }, context=ctx())
    assert res["status"] == "created", res
    return res["fact_id"]


async def test_documents_written_by_the_real_writer_are_clean_in_every_state():
    # Reviewer edit (facts S3, T4a): one fact per state, each written by the writer that owns that transition, and every
    # write checked to have happened. The old loop moved one fact through every state, but since S2 the consolidator only
    # updates a current fact, so after ARCHIVED the other updates failed unseen and only `current` and `archived` were
    # ever audited. STALE is the janitor's alone since S3 (D1, S3-D3): its writer is the repository's stale_due_fact,
    # applying the janitor's `stale_fields` (facts S4 T3c, reviewer edit).
    store = FactStore()
    fid = await _created(store, EXPIRY_NONE)
    assert check_fact(fid, store.docs[fid]) == [], "current"
    for state in ("ARCHIVED", "INVALIDATED"):
        fid = await _created(store, EXPIRY_NONE)
        res = await store.adapter.update_fact(fid, {"state": state}, ACC, context=ctx())
        assert res["status"] == "updated" and store.docs[fid]["state"] == state.lower(), res
        assert check_fact(fid, store.docs[fid]) == [], state
    fid = await _created(store, EXPIRY_NONE)
    res = await store.adapter.update_fact(fid, {"content": "mother is Mariya", "expiry": EXPIRY_NONE}, ACC, context=ctx())
    assert res["status"] == "updated" and store.docs[fid]["state"] == "superseded", res
    assert check_fact(fid, store.docs[fid]) == [] and check_fact(res["fact_id"], store.docs[res["fact_id"]]) == []
    fid = await _created(store, horizon(30))
    later = datetime.now(timezone.utc) + timedelta(days=31)
    [due] = [d for d in await store.repo.find_due_facts(later) if d.fact_id == fid]
    await store.repo.stale_due_fact(due, stale_fields(due, later))
    assert store.docs[fid]["state"] == "stale"
    assert check_fact(fid, store.docs[fid]) == [], "stale"


def test_the_document_is_not_modified():
    doc = without("account_id", "created_by_user_id", owner_id=ACC, vector=Vector([0.1]))
    before = copy.deepcopy({k: v for k, v in doc.items() if k != "vector"})
    check_fact("f1", doc)
    assert {k: v for k, v in doc.items() if k != "vector"} == before and isinstance(doc["vector"], Vector)


# ------------------------------------------------------------------------------ one violation, exactly one class

def test_id_field_different_from_the_document_id():
    assert check_fact("f1", raw(id="other")) == ["id_mismatch"]


def test_missing_id_field():
    assert check_fact("f1", without("id")) == ["id_mismatch"]


def test_legacy_owner_id_only_document_has_no_account():
    doc = without("account_id", "created_by_user_id", owner_id=ACC)
    assert check_fact("f1", doc) == ["missing_account_id"]


def test_account_and_owner_without_creator_reads_as_migrated_and_is_clean():
    assert check_fact("f1", without("created_by_user_id", owner_id=ACC)) == []


def test_owner_id_beside_both_new_fields_is_not_migrated_and_is_clean():
    assert check_fact("f1", raw(owner_id="usr-0")) == []


def test_legacy_private_visibility_is_migrated_only_on_the_legacy_branch():
    legacy = without("account_id", "created_by_user_id", owner_id=ACC, visibility="private")
    assert check_fact("f1", legacy) == ["missing_account_id"]
    assert check_fact("f1", raw(visibility="private")) == ["invalid_entity:visibility"]


def test_document_with_no_account_and_no_owner():
    assert check_fact("f1", without("account_id")) == ["missing_account_id"]


def test_missing_state_matches_no_state_filtered_read():
    assert check_fact("f1", without("state")) == ["missing_state"]


def test_null_state_is_missing():
    assert check_fact("f1", raw(state=None)) == ["missing_state"]


def test_state_that_is_not_a_fact_state():
    assert check_fact("f1", raw(state="bogus")) == ["invalid_state"]


def test_unhashable_state_is_invalid_not_a_crash():
    assert check_fact("f1", raw(state=["current"])) == ["invalid_state"]


@pytest.mark.parametrize("state", STATES)
def test_is_current_that_disagrees_with_the_state(state):
    is_current, closes = _LIFECYCLE[FactState(state)]
    doc = raw(state=state, is_current=not is_current, valid_to=NOW if closes else None)
    assert check_fact("f1", doc) == ["is_current_mismatch"]


@pytest.mark.parametrize("state", STATES)
def test_valid_to_that_disagrees_with_the_state(state):
    is_current, closes = _LIFECYCLE[FactState(state)]
    doc = raw(state=state, is_current=is_current, valid_to=None if closes else NOW)
    assert check_fact("f1", doc) == ["valid_to_mismatch"]


def test_absent_is_current_disagrees():
    assert check_fact("f1", without("is_current")) == ["is_current_mismatch"]


def test_missing_created_at():
    assert check_fact("f1", without("created_at")) == ["missing_created_at"]


def test_reported_date_stored_as_a_string():
    assert check_fact("f1", raw(reported_date="2026-03-04T10:00:00Z")) == ["reported_date_not_timestamp"]


def test_absent_reported_date_is_not_a_violation():
    assert check_fact("f1", without("reported_date")) == []


@pytest.mark.parametrize("name", ["vector", "tags_vector", "metadata_vector"])
def test_missing_vector(name):
    assert check_fact("f1", without(name)) == [f"missing_{name}"]


@pytest.mark.parametrize("name", ["vector", "tags_vector", "metadata_vector"])
@pytest.mark.parametrize("empty", [[], Vector([])])
def test_empty_vector_counts_as_missing(name, empty):
    assert check_fact("f1", raw(**{name: empty})) == [f"missing_{name}"]


@pytest.mark.parametrize("name", ["vector", "tags_vector", "metadata_vector"])
def test_plain_array_vector_is_reported_because_find_nearest_cannot_see_it(name):
    assert check_fact("f1", raw(**{name: [0.1, 0.2]})) == [f"missing_{name}"]


def test_null_vector_counts_as_missing():
    assert check_fact("f1", raw(tags_vector=None)) == ["missing_tags_vector"]


def test_document_that_does_not_validate_as_a_fact_entity():
    assert check_fact("f1", raw(type="bogus")) == ["invalid_entity:type"]


def test_missing_required_fields_are_reported_per_field():
    assert check_fact("f1", without("lineage_id", "text")) == ["invalid_entity:lineage_id", "invalid_entity:text"]


def test_account_present_but_creator_missing_does_not_validate():
    assert check_fact("f1", without("created_by_user_id")) == ["invalid_entity:created_by_user_id"]


def test_the_report_never_carries_field_values():
    classes = check_fact("f1", raw(type="SECRET-TYPE-VALUE", state="SECRET-STATE-VALUE"))
    assert classes == ["invalid_state", "invalid_entity:type"]
    assert "SECRET" not in repr(classes)


def test_independent_violations_are_all_reported():
    doc = without("created_at", "tags_vector", id="other", is_current=False)
    assert check_fact("f1", doc) == ["id_mismatch", "is_current_mismatch", "missing_created_at", "missing_tags_vector"]


# --------------------------------------------------------------------------------------------------- lineages

def lineage(*flags, lineage_id="L1", prefix="d"):
    return [(f"{prefix}{i}", {"lineage_id": lineage_id, "is_current": flag}) for i, flag in enumerate(flags)]


def test_lineage_with_exactly_one_current_document_is_clean():
    assert check_lineages(lineage(False, True, False)) == {}


def test_lineage_of_only_superseded_documents_is_clean():
    # merge_facts supersedes the sources in their own lineages and writes the merged fact under a new one
    assert check_lineages(lineage(False, False)) == {}


def test_lineage_with_two_current_documents():
    assert check_lineages(lineage(True, False, True)) == {"lineage_multiple_current": ["d0", "d1", "d2"]}


def test_lineages_are_judged_separately_and_only_the_broken_ones_are_listed():
    docs = lineage(True, False, lineage_id="A", prefix="a") + lineage(True, True, lineage_id="B", prefix="b") + \
        lineage(False, lineage_id="C", prefix="c")
    assert check_lineages(docs) == {"lineage_multiple_current": ["b0", "b1"]}


def test_is_current_must_be_true_not_merely_truthy():
    docs = [("d0", {"lineage_id": "L1", "is_current": "yes"}), ("d1", {"lineage_id": "L1", "is_current": True})]
    assert check_lineages(docs) == {}


def test_documents_without_a_lineage_are_left_to_the_entity_check():
    assert check_lineages([("d0", {"is_current": True}), ("d1", {"lineage_id": None, "is_current": True})]) == {}


def test_lineages_accept_a_generator():
    assert check_lineages(iter(lineage(True))) == {}
