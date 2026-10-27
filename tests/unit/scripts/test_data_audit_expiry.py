"""scripts/data_audit: the expiry invariants (Facts S5, C3), per class and through the run."""
import ast
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from google.cloud.firestore_v1.vector import Vector

from scripts.data_audit import run as runner
from scripts.data_audit.facts import EXPIRY_REQUIRED_CLASS, SYSTEM_ACCOUNT, check_expiry
from src.config.environment import EnvironmentConfig
from src.domain.fact_lifecycle import REVIEW_ACCEPTS, horizon_from_now, revival_fields
from src.domain.fact_lifecycle_sweep import stale_fields
from src.domain.fact_review_question import OPEN_STATUSES
from src.domain.fact_review_rules import queued_fields
from tests.support.fact_kit import put_raw
from tests.support.fact_store import FACTS, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon, on
from tests.support.firestore_eval import EvalDb

ACC = "acc-1"
REPORTED = datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
DUE = REPORTED + timedelta(days=30)
RAN_AT = datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc)
QUESTIONS = "eval_fact_review_questions"
PAST = "current_past_expiry_not_waiting"
HORIZON_CLASS = "horizon_not_counted_from_reported_date"
REPO_ROOT = Path(__file__).resolve().parents[3]


def stored(**over):
    """A current fact as stored, with the expiry fields of a 30-day horizon."""
    doc = {
        "id": "f1", "account_id": ACC, "state": "current", "domain": "biographical", "type": "event", "tags": ["family"],
        "reported_date": REPORTED, "expiry_kind": "horizon", "ttl_days": 30,
        "expires_at": DUE, "stale_since": None,
    }
    doc.update(over)
    return doc


NO_EXPIRY = {"expiry_kind": None, "ttl_days": None, "expires_at": None}
NONE_KIND = {"expiry_kind": "none", "ttl_days": None, "expires_at": None}


# ------------------------------------------------------------------------------ one violation, one clean twin

def test_a_clean_horizon_has_no_violation():
    assert check_expiry("f1", stored()) == []


def test_current_without_expiry_kind():
    assert check_expiry("f1", stored(**NO_EXPIRY)) == [EXPIRY_REQUIRED_CLASS]
    assert check_expiry("f1", stored(**NONE_KIND)) == []


@pytest.mark.parametrize("over", [
    {"account_id": SYSTEM_ACCOUNT}, {"account_id": None}, {"account_id": ""}, {"state": "stale"}, {"state": "archived"},
])
def test_only_a_current_account_fact_needs_an_expiry_kind(over):
    assert check_expiry("f1", stored(**NO_EXPIRY, **over)) == []


@pytest.mark.parametrize("pin", [
    {"domain": "agent_directive"}, {"type": "principle"}, {"tags": ["Mindset"]},
])
def test_pinned_fact_with_an_expiry(pin):
    assert check_expiry("f1", stored(**pin)) == ["pinned_expiry_not_none"]
    assert check_expiry("f1", stored(**NONE_KIND, **pin)) == []


def test_pinned_expiry_is_judged_on_current_facts_only():
    assert check_expiry("f1", stored(state="archived", type="principle")) == []


def test_unreadable_domain_type_and_tags_read_as_not_pinned():
    assert check_expiry("f1", stored(domain="bogus", type=None, tags="family")) == []
    assert check_expiry("f1", stored(domain=["x"], tags=[1, None])) == []


def test_current_with_stale_since():
    assert check_expiry("f1", stored(stale_since=REPORTED)) == ["current_with_stale_since"]
    assert check_expiry("f1", stored(state="stale", stale_since=REPORTED)) == []


@pytest.mark.parametrize("over", [
    {"ttl_days": None}, {"ttl_days": 0}, {"ttl_days": True}, {"ttl_days": 30.0}, {"expires_at": None},
    {"expires_at": "2026-04-03"}, {"reported_date": None}, {"reported_date": "2026-03-04"},
    {"expires_at": DUE - timedelta(seconds=1)},
])
def test_horizon_not_counted_from_reported_date(over):
    assert check_expiry("f1", stored(**over)) == [HORIZON_CLASS]


@pytest.mark.parametrize("over", [
    {"expires_at": DUE + timedelta(seconds=1)},     # later is legal: the cleanup floor, a re-mention
    {"reported_date": REPORTED.replace(tzinfo=None)},     # a naive stamp reads as UTC
    {"ttl_days": 1, "expires_at": REPORTED + timedelta(days=1)},
])
def test_horizon_counted_from_reported_date_or_later_is_clean(over):
    assert check_expiry("f1", stored(**over)) == []


def test_the_horizon_rule_holds_in_every_state():
    assert check_expiry("f1", stored(state="archived", ttl_days=None)) == [HORIZON_CLASS]


def test_other_kinds_are_not_judged_as_horizons():
    assert check_expiry("f1", stored(expiry_kind="date", ttl_days=None, expires_at=REPORTED)) == []


def test_current_past_expiry_not_waiting():
    assert check_expiry("f1", stored(), due_as_of=DUE) == [PAST]
    assert check_expiry("f1", stored(), due_as_of=DUE - timedelta(seconds=1)) == []
    assert check_expiry("f1", stored(), due_as_of=DUE, open_fact_ids=frozenset({"f1"})) == []
    assert check_expiry("f1", stored(), due_as_of=DUE, open_fact_ids=frozenset({"other"})) == [PAST]
    assert check_expiry("f1", stored(state="stale"), due_as_of=DUE) == []
    assert check_expiry("f1", stored(**NONE_KIND), due_as_of=DUE) == []
    assert check_expiry("f1", stored(**NO_EXPIRY), due_as_of=DUE) == [EXPIRY_REQUIRED_CLASS]


def test_past_expiry_is_not_judged_without_a_run_time():
    assert check_expiry("f1", stored(), open_fact_ids=frozenset()) == []


def test_a_naive_run_time_reads_as_utc():
    assert check_expiry("f1", stored(), due_as_of=DUE.replace(tzinfo=None)) == [PAST]


def test_classes_come_in_the_documented_order():
    doc = stored(domain="agent_directive", stale_since=REPORTED, ttl_days=0)
    assert check_expiry("f1", doc, due_as_of=RAN_AT) == [
        "pinned_expiry_not_none", "current_with_stale_since", HORIZON_CLASS, PAST]
    assert check_expiry("f1", stored(**NO_EXPIRY, stale_since=REPORTED)) == [
        EXPIRY_REQUIRED_CLASS, "current_with_stale_since"]


def test_a_missing_state_is_not_current():
    assert check_expiry("f1", stored(state=None, **NO_EXPIRY)) == []


def test_the_document_is_not_modified():
    doc = stored(stale_since=REPORTED)
    before = copy.deepcopy(doc)
    check_expiry("f1", doc, due_as_of=RAN_AT)
    assert doc == before


# ------------------------------------------------------------------------------ documents from the real writer

def meta(**over):
    base = {
        "account_id": ACC, "user_id": "usr-1", "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family"], "metadata": {"name": "Maria"}, "type": "event",
    }
    base.update(over)
    return base


@pytest.mark.parametrize("expiry", [
    on((datetime.now(timezone.utc) + timedelta(days=20)).date()), horizon(30), EXPIRY_NONE,
], ids=["date", "horizon", "none"])
async def test_documents_written_by_the_real_writer_are_clean(expiry):
    store = FactStore()
    res = await store.adapter.create_fact("car service Nov 4, bring the logbook", meta(expiry=expiry), context=ctx())
    assert res["status"] == "created", res
    assert check_expiry(res["fact_id"], store.docs[res["fact_id"]]) == []


async def test_a_horizon_floored_by_a_cleanup_pass_is_clean_because_later_is_legal():
    store = FactStore()
    put_raw(store, "old-source", "seeded",
            entity={"domain": "biographical", "reported_date": datetime.now(timezone.utc) - timedelta(days=100)})
    res = await store.adapter.create_fact("a conference Nov 18-20", meta(expiry=horizon(30), derived_from="old-source"),
                                          context=ctx(actor="stage2a"))
    assert res["status"] == "created" and "had already passed" in res["note"], res
    doc = store.docs[res["fact_id"]]
    assert doc["expires_at"] > doc["reported_date"] + timedelta(days=30), "the floor really moved it later"
    assert check_expiry(res["fact_id"], doc) == []


async def test_a_written_horizon_one_second_early_is_flagged():
    store = FactStore()
    res = await store.adapter.create_fact("a cloud trial ends ~May 20", meta(expiry=horizon(30)), context=ctx())
    assert res["status"] == "created", res
    doc = dict(store.docs[res["fact_id"]])
    assert check_expiry(res["fact_id"], doc) == []
    doc["expires_at"] = doc["reported_date"] + timedelta(days=30) - timedelta(seconds=1)
    assert check_expiry(res["fact_id"], doc) == [HORIZON_CLASS]


# ------------------------------------------------------------------------------ the run

def fact(doc_id, **over):
    doc = {
        "id": doc_id, "account_id": ACC, "created_by_user_id": "usr-1", "lineage_id": f"L-{doc_id}", "text": "x",
        "type": "event", "state": "current", "is_current": True, "valid_to": None, "domain": "biographical",
        "tags": [], "vector": Vector([0.1]), "tags_vector": Vector([0.2]), "metadata_vector": Vector([0.3]),
        "created_at": REPORTED, "reported_date": REPORTED, "valid_from": REPORTED, "last_updated": REPORTED,
        "expiry_kind": "horizon", "ttl_days": 30, "expires_at": DUE, "stale_since": None,
    }
    doc.update(over)
    return doc


def question(fact_id, status):
    return {"fact_id": fact_id, "status": status, "quoted_text": "SECRET-QUOTE"}


def seeded():
    db = EvalDb()
    docs = db.collection(FACTS).docs
    docs["plain"] = fact("plain")                                  # due, no question
    docs["noexp"] = fact("noexp", **NO_EXPIRY)
    docs["asked"] = fact("asked")                                  # due, an asked question
    docs["queued"] = fact("queued")                                # due, a queued question
    docs["closedq"] = fact("closedq")                              # due, only a closed question
    docs["early"] = fact("early", reported_date=RAN_AT, ttl_days=1, expires_at=RAN_AT + timedelta(days=1))
    qs = db.collection(QUESTIONS).docs
    qs["q1"], qs["q2"], qs["q3"] = question("asked", "asked"), question("queued", "queued"), question("closedq", "closed")
    return db


async def run(db, tmp_path, **kw):
    path = await runner.run(db, "facts", FACTS, tmp_path, now=RAN_AT, **kw)
    return json.loads(path.read_text(encoding="utf-8"))


async def test_by_default_a_current_fact_without_expiry_kind_is_counted_not_a_violation(tmp_path):
    report = await run(seeded(), tmp_path)
    assert report["classes"] == {} and report["expiry_required"] is False and report["janitor_ran_at"] is None
    assert report["counted"] == {EXPIRY_REQUIRED_CLASS: {"count": 1, "doc_ids": ["noexp"]}}
    assert report["warnings"] == []


async def test_with_expiry_required_it_is_a_violation(tmp_path):
    report = await run(seeded(), tmp_path, expiry_required=True)
    assert report["classes"] == {EXPIRY_REQUIRED_CLASS: {"count": 1, "doc_ids": ["noexp"]}}
    assert report["counted"] == {} and report["expiry_required"] is True


async def test_open_questions_hold_their_facts_and_a_closed_one_does_not(tmp_path):
    report = await run(seeded(), tmp_path, janitor_ran_at=RAN_AT, questions_collection=QUESTIONS)
    [(cls, found)] = report["classes"].items()
    assert cls == PAST and sorted(found["doc_ids"]) == ["closedq", "plain"] and found["count"] == 2
    assert report["janitor_ran_at"] == RAN_AT.isoformat()
    assert any("deferred (pool cap)" in w and "breaker" in w for w in report["warnings"])


async def test_every_open_status_holds_its_fact(tmp_path):
    db = EvalDb()
    for i, status in enumerate(s.value for s in OPEN_STATUSES):
        db.collection(FACTS).docs[f"f{i}"] = fact(f"f{i}")
        db.collection(QUESTIONS).docs[f"q{i}"] = question(f"f{i}", status)
    report = await run(db, tmp_path, janitor_ran_at=RAN_AT, questions_collection=QUESTIONS)
    assert report["classes"] == {}


async def test_the_questions_collection_is_not_queried_without_a_run_time(tmp_path):
    db = seeded()
    report = await run(db, tmp_path)
    assert db.collection(QUESTIONS).executed == []
    assert PAST not in report["classes"] and PAST not in report["counted"]
    assert not any("deferred" in w for w in report["warnings"])


async def test_a_run_time_without_a_questions_collection_is_refused(tmp_path):
    with pytest.raises(ValueError):
        await runner.run(seeded(), "facts", FACTS, tmp_path, now=RAN_AT, janitor_ran_at=RAN_AT)


async def test_the_run_writes_nothing_and_prints_counts_only(tmp_path, capsys):
    db = seeded()
    before = {name: (copy.deepcopy(col.docs), dict(col.versions)) for name, col in db.cols.items()}
    path = await runner.run(db, "facts", FACTS, tmp_path, now=RAN_AT, expiry_required=True, janitor_ran_at=RAN_AT,
                            questions_collection=QUESTIONS)
    assert {name: (col.docs, col.versions) for name, col in db.cols.items()} == before and db.commits == []
    assert list(tmp_path.iterdir()) == [path]
    out = capsys.readouterr().out.replace(path.name, "")
    assert f"{PAST}: 2" in out and f"{EXPIRY_REQUIRED_CLASS}: 1" in out and "warning:" in out
    for hidden in ("plain", "closedq", "noexp", "SECRET-QUOTE", ACC):
        assert hidden not in out
    assert "SECRET-QUOTE" not in path.read_text(encoding="utf-8")


def test_the_checker_entry_names_the_questions_property():
    assert isinstance(getattr(EnvironmentConfig, runner.CHECKERS["facts"]["questions"]), property)
    assert runner.CHECKERS["facts"]["expiry"] is check_expiry


def test_a_bad_janitor_time_is_refused_by_the_command_line():
    with pytest.raises(SystemExit):
        runner.main(["facts", "--janitor-ran-at", "nonsense"])


def test_the_modules_call_no_write_method():
    writes = {"set", "update", "delete", "batch", "transaction", "create", "add", "commit"}
    for name in ("run.py", "facts.py"):
        tree = ast.parse((REPO_ROOT / "scripts" / "data_audit" / name).read_text(encoding="utf-8"))
        called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not called & writes, (name, called & writes)


# ------------------------------------------------------------------------------ pins stored in upper case

@pytest.mark.parametrize("pin", [{"domain": "AGENT_DIRECTIVE"}, {"type": "PRINCIPLE"}])
def test_an_upper_case_stored_pin_still_counts_as_pinned(pin):
    assert check_expiry("f1", stored(**pin)) == ["pinned_expiry_not_none"]
    assert check_expiry("f1", stored(**NONE_KIND, **pin)) == []


# ------------------------------------------------------------------------------ documents the lifecycle writers leave

async def _horizon_fact(store):
    res = await store.adapter.create_fact("practises guitar 1 h/day", meta(expiry=horizon(30)), context=ctx())
    assert res["status"] == "created", res
    return res["fact_id"]


async def _staled(store):
    """A horizon fact the janitor staled a month on, read back with the review's token."""
    fid = await _horizon_fact(store)
    later = datetime.now(timezone.utc) + timedelta(days=31)
    [due] = [d for d in await store.repo.find_due_facts(later) if d.fact_id == fid]
    await store.repo.stale_due_fact(due, stale_fields(due, later))
    stored_fact = await store.repo.resolve_current(fid, ACC, REVIEW_ACCEPTS)
    assert stored_fact is not None and store.docs[fid]["state"] == "stale"
    return fid, stored_fact, later


async def test_a_horizon_fact_remembered_with_a_later_reported_date_is_clean():
    store = FactStore()
    fid = await _horizon_fact(store)
    first = datetime.now(timezone.utc) - timedelta(days=20)      # the fact was first reported 20 days ago
    store.docs[fid]["reported_date"] = first
    store.docs[fid]["expires_at"] = first + timedelta(days=30)
    assert check_expiry(fid, store.docs[fid]) == []
    later = datetime.now(timezone.utc) - timedelta(days=1)
    res = await store.adapter.update_fact(fid, {"reported_date": later.isoformat()}, ACC, context=ctx())
    assert res["status"] == "updated", res
    doc = store.docs[fid]
    assert doc["reported_date"] == later and doc["expires_at"] == later + timedelta(days=30)
    assert check_expiry(fid, doc) == []


async def test_a_revived_fact_is_clean_and_not_due():
    store = FactStore()
    fid, stored_fact, now = await _staled(store)
    fact_entity = stored_fact.fact
    expiry_fields = horizon_from_now(30, fact_entity.reported_date, now)
    await store.repo.update_fact_fields(
        stored_fact, revival_fields(fact_entity, expiry_fields, now, "owner", "the owner confirmed it"))
    doc = store.docs[fid]
    assert doc["state"] == "current" and doc["stale_since"] is None and doc["expires_at"] > now
    assert check_expiry(fid, doc, due_as_of=now) == []


async def test_a_queued_fact_is_flagged_past_expiry_only_without_its_open_question():
    store = FactStore()
    fid, stored_fact, now = await _staled(store)
    await store.repo.update_fact_fields(stored_fact, queued_fields(stored_fact.fact, "q-1", now))
    doc = store.docs[fid]
    assert doc["state"] == "current" and doc["stale_since"] is None and doc["expires_at"] <= now
    assert check_expiry(fid, doc) == []
    assert check_expiry(fid, doc, due_as_of=now, open_fact_ids=frozenset({fid})) == []
    assert check_expiry(fid, doc, due_as_of=now, open_fact_ids=frozenset({"q-1"})) == [PAST]
    assert check_expiry(fid, doc, due_as_of=now) == [PAST]
