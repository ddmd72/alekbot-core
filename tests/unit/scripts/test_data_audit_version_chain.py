"""scripts/data_audit: the version-chain classes (`superseded_by`, `replaces_fact_id`, `version`) on stored documents."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.data_audit import run as runner
from scripts.data_audit.facts import COUNTED_CLASSES, check_across, check_fact, check_version_chain
from src.domain.entities import FactState
from src.domain.exceptions import FactResolutionError
from src.domain.fact_lifecycle import CONSOLIDATION_ACCEPTS, superseded_closure
from tests.support.fact_kit import ACC, OTHER, T0, make_fact, put_raw
from tests.support.fact_store import FACTS, FactStore, embed

CUTOFF = datetime(2026, 3, 1, tzinfo=timezone.utc)
BEFORE = CUTOFF - timedelta(days=30)
AFTER = CUTOFF + timedelta(days=30)
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def d(doc_id, lineage="L1", version=1, account=ACC, **over):
    """The fields the version-chain checks read, as `run.py` keeps them."""
    doc = {
        "lineage_id": lineage, "version": version, "account_id": account, "state": "current", "is_current": True,
        "superseded_by": None, "replaces_fact_id": None, "last_updated": AFTER,
    }
    doc.update(over)
    return doc_id, doc


def closed(doc_id, successor, **over):
    return d(doc_id, state="superseded", is_current=False, superseded_by=successor, **over)


def chain(*members, cutoff=CUTOFF):
    return check_version_chain(list(members), cutoff)


# ------------------------------------------------------------------------------------------------ clean shapes

def test_a_version_pair_and_a_merge_are_clean():
    docs = [
        closed("v1", "v2"), d("v2", version=2, replaces_fact_id="v1"),                  # a version: same lineage
        closed("a", "m", lineage="LA"), closed("b", "m", lineage="LB"), d("m", lineage="LM"),   # a merge: new lineage
    ]
    assert chain(*docs) == {}


def test_documents_without_any_pointer_are_clean():
    assert chain(d("a", lineage="LA"), d("b", lineage="LB")) == {} and chain() == {}


def test_a_generator_is_accepted():
    assert check_version_chain(iter([d("a")]), CUTOFF) == {}


# ------------------------------------------------------------------------------------------ one test per class

def test_superseded_by_naming_a_document_that_does_not_exist():
    assert chain(closed("a", "gone")) == {"superseded_by_dangling": ["a"]}


def test_superseded_by_naming_a_document_of_another_account():
    assert chain(closed("a", "m"), d("m", lineage="LM", account=OTHER)) == {"superseded_by_dangling": ["a"]}


def test_superseded_by_is_checked_whatever_the_state():
    docs = [d("a", state="invalidated", superseded_by="gone")]
    assert chain(*docs) == {"superseded_by_dangling": ["a"], "superseded_by_on_open_doc": ["a"]}


def test_replaces_fact_id_naming_a_document_that_does_not_exist():
    assert chain(d("v2", version=2, replaces_fact_id="gone")) == {"replaces_target_missing": ["v2"]}


def test_replaces_fact_id_naming_a_document_of_another_lineage():
    docs = [closed("v1", "v2", lineage="OTHER"), d("v2", version=2, replaces_fact_id="v1")]
    assert chain(*docs) == {"replaces_lineage_mismatch": ["v2"]}


@pytest.mark.parametrize("older, newer", [(1, 3), (1, None), (None, 2), ("1", 2)])
def test_replaces_fact_id_naming_a_document_that_is_not_version_minus_one(older, newer):
    docs = [closed("v1", "v2", version=older), d("v2", version=newer, replaces_fact_id="v1")]
    assert chain(*docs) == {"replaces_version_mismatch": ["v2"]}


def test_a_version_pointing_at_a_document_of_its_own_version_is_also_not_unique():
    docs = [closed("v1", "v2", version=2), d("v2", version=2, replaces_fact_id="v1")]
    assert chain(*docs) == {"replaces_version_mismatch": ["v2"], "version_not_unique": ["v1", "v2"]}


@pytest.mark.parametrize("back", [None, "elsewhere"])
def test_replaces_fact_id_whose_target_does_not_point_back(back):
    docs = [d("v1", superseded_by=back, state="superseded", is_current=False), d("v2", version=2, replaces_fact_id="v1")]
    expected = {"replaces_not_pointing_back": ["v2"]}
    # a closed document with no successor is a second finding; one with a foreign successor is a dangling one
    expected = {("superseded_by_dangling" if back else "superseded_without_successor"): ["v1"], **expected}
    assert chain(*docs) == expected


def test_two_documents_of_one_lineage_with_the_same_version():
    docs = [d("a", version=2), d("b", version=2), d("c", lineage="L2", version=3)]
    assert chain(*docs) == {"version_not_unique": ["a", "b"]}


def test_the_same_version_in_different_lineages_is_clean():
    assert chain(d("a", lineage="LA"), d("b", lineage="LB")) == {}


def test_documents_without_a_version_or_lineage_are_not_compared():
    docs = [d("a", version=None), d("b", version=None), d("c", lineage=None), d("e", lineage=None)]
    assert chain(*docs) == {}


def test_superseded_without_successor_written_after_the_cutoff():
    docs = [d("a", state="superseded", is_current=False, last_updated=AFTER)]
    assert chain(*docs) == {"superseded_without_successor": ["a"]}


# ------------------------------------------------------------------------------------------- the lineage head

def test_a_lineage_whose_highest_version_is_closed_while_a_lower_one_is_open():
    docs = [d("v1", version=1), closed("v2", "gone", version=2)]
    assert chain(*docs) == {"superseded_by_dangling": ["v2"], "lineage_open_below_highest_version": ["v1", "v2"]}


def test_a_closed_highest_version_with_an_open_lower_one_names_the_whole_lineage():
    docs = [d("v1", version=1), closed("v2", "v1", version=2), d("other", lineage="L2")]
    assert chain(*docs)["lineage_open_below_highest_version"] == ["v1", "v2"]


def test_a_lower_version_that_was_never_closed_below_an_open_head():
    docs = [d("v1", version=1, is_current=False, state="archived"), d("v2", version=2, replaces_fact_id="v1")]
    found = chain(*docs)
    assert found["lineage_open_below_highest_version"] == ["v1", "v2"]


def test_a_closed_version_above_an_open_lower_one_is_a_head_violation():
    docs = [closed("v1", "v2", version=3), d("v2", version=2, replaces_fact_id="v1")]
    assert set(chain(*docs)) == {"lineage_open_below_highest_version", "replaces_version_mismatch"}


def test_a_fully_superseded_lineage_is_clean():
    docs = [closed("v1", "v2", version=1), closed("v2", "m", version=2, replaces_fact_id="v1"), d("m", lineage="LM")]
    assert chain(*docs) == {}


def test_an_open_head_above_closed_versions_is_clean():
    docs = [closed("v1", "v2", version=1), closed("v2", "v3", version=2, replaces_fact_id="v1"),
            d("v3", version=3, replaces_fact_id="v2")]
    assert chain(*docs) == {}


def test_documents_without_a_version_do_not_take_part_in_the_head_rule():
    docs = [d("v1", version=None), closed("v2", "v1", version=2)]
    assert "lineage_open_below_highest_version" not in chain(*docs)


# ------------------------------------------------------------------------- cycles, back-pointers, open documents

def test_a_document_pointing_at_itself():
    assert chain(closed("a", "a")) == {"superseded_by_cycle": ["a"]}


@pytest.mark.parametrize("size", [2, 3])
def test_a_cycle_of_superseded_by_pointers_across_lineages(size):
    ids = [f"c{i}" for i in range(size)]
    docs = [closed(ids[i], ids[(i + 1) % size], lineage=f"L{i}") for i in range(size)]
    assert chain(*docs) == {"superseded_by_cycle": ids}


def test_only_the_documents_on_the_cycle_are_listed_not_those_leading_into_it():
    docs = [closed("in", "a", lineage="L0"), closed("a", "b", lineage="L1"), closed("b", "a", lineage="L2")]
    assert chain(*docs) == {"superseded_by_cycle": ["a", "b"]}


def test_a_long_acyclic_superseded_by_chain_is_clean():
    docs = [closed(f"m{i}", f"m{i + 1}", lineage=f"L{i}") for i in range(60)] + [d("m60", lineage="L60")]
    assert chain(*docs) == {}


def test_a_pointer_that_is_not_a_string_is_dangling_not_a_crash():
    docs = [closed("a", ["x"]), d("b", lineage="L2", replaces_fact_id={"y": 1})]
    assert chain(*docs) == {"superseded_by_dangling": ["a"], "replaces_target_missing": ["b"]}


def test_a_same_lineage_successor_that_does_not_name_the_document():
    docs = [closed("v1", "v2"), d("v2", version=2)]
    assert chain(*docs) == {"superseded_by_not_pointing_back": ["v1"]}


def test_a_same_lineage_successor_naming_another_document():
    docs = [closed("v1", "v2"), d("v2", version=2, replaces_fact_id="elsewhere")]
    assert set(chain(*docs)) == {"superseded_by_not_pointing_back", "replaces_target_missing"}


def test_a_merge_successor_in_another_lineage_needs_no_back_pointer():
    assert chain(closed("a", "m", lineage="LA"), d("m", lineage="LM")) == {}


def test_a_current_document_with_a_superseded_by():
    docs = [d("a", lineage="LA", superseded_by="m"), d("m", lineage="LM")]
    assert chain(*docs) == {"superseded_by_on_open_doc": ["a"]}


@pytest.mark.parametrize("state", ["invalidated", "archived", "stale", "current"])
def test_every_state_but_superseded_with_a_superseded_by_is_an_open_document(state):
    docs = [d("a", lineage="LA", state=state, is_current=False, superseded_by="m"), d("m", lineage="LM")]
    assert chain(*docs) == {"superseded_by_on_open_doc": ["a"]}


def test_a_superseded_document_with_a_superseded_by_is_not_an_open_document():
    assert "superseded_by_on_open_doc" not in chain(closed("a", "m", lineage="LA"), d("m", lineage="LM"))


def test_an_unreadable_state_is_not_reported_as_an_open_document():
    assert chain(d("a", lineage="LA", state="bogus", superseded_by="m"), d("m", lineage="LM")) == {}


# --------------------------------------------------------------------------------------- last_updated per document

def stored(**over):
    holder = FactStore()
    put_raw(holder, "f1", "x")
    return {**holder.docs["f1"], **over}


def test_a_stored_document_has_a_timestamp_last_updated():
    assert check_fact("f1", stored()) == []


@pytest.mark.parametrize("value", ["2026-04-01T00:00:00Z", 5])
def test_last_updated_that_is_not_a_timestamp(value):
    # pydantic coerces these, so only this class reports them
    assert check_fact("f1", stored(last_updated=value)) == ["last_updated_not_timestamp"]


def test_a_null_last_updated_is_reported_beside_the_entity_error():
    assert check_fact("f1", stored(last_updated=None)) == ["last_updated_not_timestamp", "invalid_entity:last_updated"]


def test_a_missing_last_updated_is_reported():
    doc = stored()
    del doc["last_updated"]
    assert check_fact("f1", doc) == ["last_updated_not_timestamp"]


# ------------------------------------------------------------------------------------- the cutoff counting rule

def test_superseded_without_successor_before_the_cutoff_is_counted_not_flagged():
    docs = [d("a", state="superseded", is_current=False, last_updated=BEFORE)]
    assert chain(*docs) == {"superseded_without_successor_pre_s2": ["a"]}
    assert "superseded_without_successor_pre_s2" in COUNTED_CLASSES and "superseded_without_successor" not in COUNTED_CLASSES


def test_a_write_exactly_at_the_cutoff_is_after_it():
    docs = [d("a", state="superseded", is_current=False, last_updated=CUTOFF)]
    assert chain(*docs) == {"superseded_without_successor": ["a"]}


def test_without_a_cutoff_every_such_document_is_counted():
    docs = [d("a", state="superseded", is_current=False, last_updated=AFTER)]
    assert check_version_chain(docs, None) == {"superseded_without_successor_pre_s2": ["a"]}


@pytest.mark.parametrize("moment", [None, "2026-04-01", 5])
def test_a_missing_or_malformed_last_updated_counts_as_pre_s2(moment):
    docs = [d("a", state="superseded", is_current=False, last_updated=moment)]
    assert chain(*docs) == {"superseded_without_successor_pre_s2": ["a"]}


def test_a_naive_timestamp_reads_as_utc():
    naive = AFTER.replace(tzinfo=None)
    docs = [d("a", state="superseded", is_current=False, last_updated=naive)]
    assert chain(*docs) == {"superseded_without_successor": ["a"]}
    assert check_version_chain(docs, CUTOFF.replace(tzinfo=None)) == {"superseded_without_successor": ["a"]}


def test_a_closed_document_with_a_successor_is_clean_whenever_it_was_written():
    assert chain(closed("a", "m", last_updated=AFTER, lineage="LA"), d("m", lineage="LM")) == {}


def test_only_the_superseded_state_needs_a_successor():
    docs = [d(f"s-{state}", lineage=state, state=state, is_current=False) for state in ("archived", "invalidated", "stale")]
    assert chain(*docs) == {}


def test_a_state_that_is_not_a_fact_state_is_left_to_check_fact():
    assert chain(d("a", lineage="LA", state="bogus"), d("b", lineage="LB", state=["superseded"])) == {}


def test_the_checks_do_not_modify_the_documents():
    docs = [closed("v1", "v2"), d("v2", version=2, replaces_fact_id="v1")]
    before = [(i, dict(doc)) for i, doc in docs]
    chain(*docs)
    assert docs == before


# --------------------------------------------------------------------------------------------- check_across

def test_check_across_reports_the_lineage_class_beside_the_chain_classes():
    docs = [d("a", is_current=True), d("b", version=2, is_current=True), closed("c", "gone", lineage="L9")]
    assert check_across(docs, CUTOFF) == {
        "lineage_multiple_current": ["a", "b"], "superseded_by_dangling": ["c"],
        "lineage_open_below_highest_version": ["a", "b"],
    }


def test_check_across_accepts_a_generator_and_no_cutoff():
    assert check_across(iter([d("a")])) == {}


# ------------------------------------------------------------------------------------------------ the runner

async def test_the_runner_reports_counted_documents_apart_from_violations(tmp_path):
    store = FactStore()
    store.docs.update({
        "ok": {**_stored("ok"), "state": "current"},
        "old": {**_stored("old"), "state": "superseded", "is_current": False, "valid_to": BEFORE, "last_updated": BEFORE},
        "new": {**_stored("new"), "state": "superseded", "is_current": False, "valid_to": AFTER, "last_updated": AFTER},
    })
    report = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW, s2_cutoff=CUTOFF)).read_text())
    assert report["s2_cutoff"] == CUTOFF.isoformat()
    assert report["classes"] == {"superseded_without_successor": {"count": 1, "doc_ids": ["new"]}}
    # Reviewer edit (facts S5, C3; plan_design §3): without --expiry-required a current fact with no expiry_kind is
    # counted, so the current "ok" is now counted too, beside the pre-S2 entry, and still never a violation.
    assert report["counted"] == {
        "current_without_expiry_kind": {"count": 1, "doc_ids": ["ok"]},
        "superseded_without_successor_pre_s2": {"count": 1, "doc_ids": ["old"]},
    }


async def test_the_runner_without_a_cutoff_flags_nothing_of_that_kind(tmp_path, capsys):
    store = FactStore()
    store.docs["new"] = {**_stored("new"), "state": "superseded", "is_current": False, "valid_to": AFTER}
    report = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW)).read_text())
    assert report["s2_cutoff"] is None and report["classes"] == {}
    assert report["counted"]["superseded_without_successor_pre_s2"]["count"] == 1
    out = capsys.readouterr().out
    assert "superseded_without_successor_pre_s2: 1 (counted, not a violation)" in out and "new" not in out.replace("data_audit", "")


async def test_the_runner_warns_when_pointers_exist_and_no_cutoff_was_given(tmp_path, capsys):
    store = FactStore()
    store.docs["a"] = {**_stored("a"), "state": "superseded", "is_current": False, "valid_to": AFTER, "superseded_by": "b"}
    store.docs["b"] = _stored("b")
    report = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW)).read_text())
    assert report["warnings"] == ["superseded_without_successor not judged: version pointers exist, pass --s2-cutoff"]
    assert "warning: superseded_without_successor not judged" in capsys.readouterr().out


async def test_the_runner_does_not_warn_with_a_cutoff_or_before_any_pointer_exists(tmp_path, capsys):
    store = FactStore()
    store.docs["a"] = {**_stored("a"), "state": "superseded", "is_current": False, "valid_to": AFTER, "superseded_by": "b"}
    store.docs["b"] = _stored("b")
    with_cutoff = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW, s2_cutoff=CUTOFF)).read_text())
    pre_s2 = FactStore()
    pre_s2.docs["old"] = {**_stored("old"), "state": "superseded", "is_current": False, "valid_to": BEFORE}
    quiet = json.loads((await runner.run(pre_s2.db, "facts", FACTS, tmp_path, now=NOW + timedelta(seconds=1))).read_text())
    assert with_cutoff["warnings"] == [] and quiet["warnings"] == []
    assert "warning" not in capsys.readouterr().out


def _stored(doc_id):
    """A raw stored document of a current fact, written the way the repository writes it."""
    holder = FactStore()
    put_raw(holder, doc_id, doc_id, entity={"lineage_id": f"lin-{doc_id}"})
    return dict(holder.docs[doc_id])


@pytest.mark.parametrize("text, expected", [
    ("2026-03-01T00:00:00+00:00", CUTOFF), ("2026-03-01T00:00:00Z", CUTOFF), ("2026-03-01", CUTOFF),
    ("2026-03-01T02:00:00+02:00", CUTOFF),
])
def test_the_cutoff_argument_is_iso_and_a_naive_time_is_utc(text, expected):
    assert runner.parse_cutoff(text) == expected and runner.parse_cutoff(text).tzinfo is not None


def test_a_cutoff_that_is_not_a_time_is_refused_by_the_command_line():
    with pytest.raises(SystemExit):
        runner.main(["facts", "--s2-cutoff", "yesterday"])


def test_the_runner_keeps_the_fields_the_chain_checks_read():
    keep = set(runner.CHECKERS["facts"]["keep"])
    assert {"account_id", "state", "superseded_by", "replaces_fact_id", "version", "last_updated"} <= keep
    assert not keep & {"text", "vector", "tags_vector", "metadata_vector", "tags", "metadata"}


# ------------------------------------------------------------------------------ documents the real writer produces

def version_of(stored, new_id, text):
    f = stored.fact
    return f.model_copy(update=dict(
        id=new_id, text=text, version=f.version + 1, replaces_fact_id=f.id, valid_from=NOW, last_updated=NOW,
        vector=embed(text), tags_vector=embed("t " + text), metadata_vector=embed("m " + text),
    ))


async def seed_current(store, text, **over):
    await store.repo.add_fact(make_fact(text, **over))


async def supersede(store, new, replaced, actor):
    """A version or a merge as the writers store it: the closures built by `superseded_closure`, applied by the repository."""
    await store.repo.write_superseding(new, [(stored, superseded_closure(stored.fact, new, NOW, actor)) for stored in replaced])


async def current(store, fact_id):
    return await store.repo.resolve_current(fact_id, ACC, CONSOLIDATION_ACCEPTS)


async def test_documents_written_by_s2_versions_and_merges_are_clean(tmp_path):
    store = FactStore()
    await seed_current(store, "plain")
    await seed_current(store, "x")
    await seed_current(store, "src-a")
    await seed_current(store, "src-b")
    await seed_current(store, "src-c")
    # a pre-S2 document: closed with no successor, last written before the cutoff
    await store.repo.add_fact(make_fact(
        "legacy", state=FactState.SUPERSEDED, is_current=False, valid_to=T0, last_updated=T0))

    # a chain of three versions, each written by write_superseding
    await supersede(store, version_of(await current(store, "f-x"), "f-x2", "x two"), [await current(store, "f-x")], "stage1")
    await supersede(store, version_of(await current(store, "f-x2"), "f-x3", "x three"), [await current(store, "f-x2")], "stage1")
    # a merge of two sources into a new lineage, then a version of the merged fact
    sources = [await current(store, "f-src-a"), await current(store, "f-src-b")]
    await supersede(store, make_fact("merged", lineage_id="lin-merged"), sources, "stage2a")
    await supersede(
        store,
        version_of(await current(store, "f-merged"), "f-merged2", "merged two"), [await current(store, "f-merged")], "stage1")

    assert set(store.docs) == {
        "f-plain", "f-x", "f-x2", "f-x3", "f-src-a", "f-src-b", "f-src-c", "f-merged", "f-merged2", "f-legacy"}
    assert store.docs["f-x3"]["version"] == 3 and store.docs["f-x3"]["replaces_fact_id"] == "f-x2"
    assert store.docs["f-x"]["superseded_by"] == "f-x2" == store.docs["f-x3"]["replaces_fact_id"]
    assert store.docs["f-src-a"]["superseded_by"] == "f-merged" and store.docs["f-merged2"]["version"] == 2
    # Reviewer edit (facts S3, T3; plan §3.8): write_superseding now takes the actor and writes each closed document's
    # trail entry; the audit must still find documents carrying a trail clean.
    assert [e["reason"] for e in store.docs["f-x"]["lifecycle_events"]] == ["replaced by f-x2"]
    assert [e["reason"] for e in store.docs["f-src-a"]["lifecycle_events"]] == ["merged into f-merged"]
    for doc_id, doc in store.docs.items():
        assert check_fact(doc_id, doc) == [], doc_id
    kept = [(i, {k: doc.get(k) for k in runner.CHECKERS["facts"]["keep"]}) for i, doc in store.docs.items()]
    assert check_across(kept, CUTOFF) == {"superseded_without_successor_pre_s2": ["f-legacy"]}   # no head violation

    report = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW, s2_cutoff=CUTOFF)).read_text())
    assert report["documents"] == 10 and report["classes"] == {} and report["warnings"] == []
    # Reviewer edit (facts S5, C3; plan_design §3): these fixtures carry no expiry, so without --expiry-required every
    # current head is counted as current_without_expiry_kind; no closed document is, and nothing is a violation.
    assert list(report["counted"]) == ["current_without_expiry_kind", "superseded_without_successor_pre_s2"]
    assert sorted(report["counted"]["current_without_expiry_kind"]["doc_ids"]) == [
        "f-merged2", "f-plain", "f-src-c", "f-x3"]


async def test_the_audit_sees_a_pointer_broken_in_a_store_the_writers_built(tmp_path):
    store = FactStore()
    await seed_current(store, "x")
    old = await current(store, "f-x")
    await supersede(store, version_of(old, "f-x2", "x two"), [old], "stage1")
    await store.repo.facts_col.document("f-x2").delete()          # the successor of f-x is gone
    await seed_current(store, "y")
    old = await current(store, "f-y")
    await supersede(store, version_of(old, "f-y2", "y two"), [old], "stage1")
    await store.repo.facts_col.document("f-y").update({"superseded_by": None})   # f-y2 no longer pointed back to

    report = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW, s2_cutoff=CUTOFF)).read_text())

    assert report["classes"] == {
        "replaces_not_pointing_back": {"count": 1, "doc_ids": ["f-y2"]},
        "superseded_by_dangling": {"count": 1, "doc_ids": ["f-x"]},
        "superseded_without_successor": {"count": 1, "doc_ids": ["f-y"]},
    }
    with pytest.raises(FactResolutionError):
        await current(store, "f-x")
