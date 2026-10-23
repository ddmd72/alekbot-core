"""scripts/data_audit: the version-chain classes (`superseded_by`, `replaces_fact_id`, `version`) on stored documents."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.data_audit import run as runner
from scripts.data_audit.facts import COUNTED_CLASSES, check_across, check_fact, check_version_chain
from src.domain.entities import FactState
from src.domain.exceptions import FactResolutionError
from src.domain.fact_lifecycle import CONSOLIDATION_ACCEPTS
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
    assert chain(*docs) == {"superseded_by_dangling": ["a"]}


def test_replaces_fact_id_naming_a_document_that_does_not_exist():
    assert chain(d("v2", version=2, replaces_fact_id="gone")) == {"replaces_target_missing": ["v2"]}


def test_replaces_fact_id_naming_a_document_of_another_lineage():
    docs = [closed("v1", "v2", lineage="OTHER"), d("v2", version=2, replaces_fact_id="v1")]
    assert chain(*docs) == {"replaces_lineage_mismatch": ["v2"]}


@pytest.mark.parametrize("older, newer", [(1, 3), (3, 2), (1, None), (None, 2), ("1", 2)])
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
    docs = [d("a", version=2), d("b", version=2), d("c", version=3)]
    assert chain(*docs) == {"version_not_unique": ["a", "b"]}


def test_the_same_version_in_different_lineages_is_clean():
    assert chain(d("a", lineage="LA"), d("b", lineage="LB")) == {}


def test_documents_without_a_version_or_lineage_are_not_compared():
    docs = [d("a", version=None), d("b", version=None), d("c", lineage=None), d("e", lineage=None)]
    assert chain(*docs) == {}


def test_superseded_without_successor_written_after_the_cutoff():
    docs = [d("a", state="superseded", is_current=False, last_updated=AFTER)]
    assert chain(*docs) == {"superseded_without_successor": ["a"]}


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
    assert report["counted"] == {"superseded_without_successor_pre_s2": {"count": 1, "doc_ids": ["old"]}}


async def test_the_runner_without_a_cutoff_flags_nothing_of_that_kind(tmp_path, capsys):
    store = FactStore()
    store.docs["new"] = {**_stored("new"), "state": "superseded", "is_current": False, "valid_to": AFTER}
    report = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW)).read_text())
    assert report["s2_cutoff"] is None and report["classes"] == {}
    assert report["counted"]["superseded_without_successor_pre_s2"]["count"] == 1
    out = capsys.readouterr().out
    assert "superseded_without_successor_pre_s2: 1 (counted, not a violation)" in out and "new" not in out.replace("data_audit", "")


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
    await store.repo.write_superseding(version_of(await current(store, "f-x"), "f-x2", "x two"), [await current(store, "f-x")], NOW)
    await store.repo.write_superseding(version_of(await current(store, "f-x2"), "f-x3", "x three"), [await current(store, "f-x2")], NOW)
    # a merge of two sources into a new lineage, then a version of the merged fact
    sources = [await current(store, "f-src-a"), await current(store, "f-src-b")]
    await store.repo.write_superseding(make_fact("merged", lineage_id="lin-merged"), sources, NOW)
    await store.repo.write_superseding(
        version_of(await current(store, "f-merged"), "f-merged2", "merged two"), [await current(store, "f-merged")], NOW)

    assert set(store.docs) == {
        "f-plain", "f-x", "f-x2", "f-x3", "f-src-a", "f-src-b", "f-src-c", "f-merged", "f-merged2", "f-legacy"}
    assert store.docs["f-x3"]["version"] == 3 and store.docs["f-x3"]["replaces_fact_id"] == "f-x2"
    assert store.docs["f-x"]["superseded_by"] == "f-x2" == store.docs["f-x3"]["replaces_fact_id"]
    assert store.docs["f-src-a"]["superseded_by"] == "f-merged" and store.docs["f-merged2"]["version"] == 2
    for doc_id, doc in store.docs.items():
        assert check_fact(doc_id, doc) == [], doc_id
    kept = [(i, {k: doc.get(k) for k in runner.CHECKERS["facts"]["keep"]}) for i, doc in store.docs.items()]
    assert check_across(kept, CUTOFF) == {"superseded_without_successor_pre_s2": ["f-legacy"]}

    report = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW, s2_cutoff=CUTOFF)).read_text())
    assert report["documents"] == 10 and report["classes"] == {}
    assert list(report["counted"]) == ["superseded_without_successor_pre_s2"]


async def test_the_audit_sees_a_pointer_broken_in_a_store_the_writers_built(tmp_path):
    store = FactStore()
    await seed_current(store, "x")
    old = await current(store, "f-x")
    await store.repo.write_superseding(version_of(old, "f-x2", "x two"), [old], NOW)
    await store.repo.facts_col.document("f-x2").delete()          # the successor of f-x is gone
    await seed_current(store, "y")
    old = await current(store, "f-y")
    await store.repo.write_superseding(version_of(old, "f-y2", "y two"), [old], NOW)
    await store.repo.facts_col.document("f-y").update({"superseded_by": None})   # f-y2 no longer pointed back to

    report = json.loads((await runner.run(store.db, "facts", FACTS, tmp_path, now=NOW, s2_cutoff=CUTOFF)).read_text())

    assert report["classes"] == {
        "replaces_not_pointing_back": {"count": 1, "doc_ids": ["f-y2"]},
        "superseded_by_dangling": {"count": 1, "doc_ids": ["f-x"]},
        "superseded_without_successor": {"count": 1, "doc_ids": ["f-y"]},
    }
    with pytest.raises(FactResolutionError):
        await current(store, "f-x")
