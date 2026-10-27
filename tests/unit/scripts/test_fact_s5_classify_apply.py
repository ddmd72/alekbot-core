"""scripts/migration/fact_s5_classify.py apply_plan: the plan written by a dry run, replayed through the real repository on the fake."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from google.cloud.firestore_v1.vector import Vector

from scripts.consolidation import s3_dry_run_kit
from scripts.migration import fact_s5_classify as s5
from scripts.migration.fact_s5_classify_rules import PlanFileError
from src.domain.fact_lifecycle_sweep import stale_fields
from src.domain.llm import LLMResponse
from src.domain.owner_timezone import ResolvedOwnerZone
from tests.support.clock_instants import BERLIN
from tests.support.fact_store import FACTS, FactStore

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
REPORTED = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
TRIAL_REPORTED = datetime(2026, 4, 1, 9, 30, tzinfo=timezone.utc)
ACC = "acc-owner-0001"

CAR = "car service Nov 4, bring the logbook"
PIPE = "refactoring the doc pipeline"
TRIAL = "a cloud trial ended May 20"
MARIA = "mother is Maria"
ANSWERS = {
    CAR: {"kind": "date", "date": "2026-11-04"},
    PIPE: {"kind": "horizon", "days": 30},
    TRIAL: {"kind": "date", "date": "2026-05-20"},
    MARIA: {"kind": "none"},
}
NEW_KEYS = {"expiry_kind", "ttl_days", "expires_at", "lifecycle_events"}


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_dry_run_kit, "MEMORY_DIR", tmp_path)
    return tmp_path


def raw(doc_id, text, **over):
    doc = {
        "id": doc_id, "account_id": ACC, "created_by_user_id": "usr-1", "lineage_id": f"L-{doc_id}",
        "text": text, "type": "event", "domain": "possession", "temporal_class": "dynamic", "tags": ["t"],
        "metadata": {"k": "v"}, "state": "current", "is_current": True, "valid_to": None,
        "vector": Vector([0.1]), "tags_vector": Vector([0.2]), "metadata_vector": Vector([0.3]),
        "created_at": REPORTED, "reported_date": REPORTED, "valid_from": REPORTED, "last_updated": REPORTED,
    }
    doc.update(over)
    return doc


async def owner_of(account_id):
    return ResolvedOwnerZone("owner-1", BERLIN)


def scripted(table):
    async def call(request):
        lines = [json.loads(line) for line in request.messages[0].parts[0].text.split("\n") if line.startswith("{")]
        decisions = [{"n": l["n"], "unsure": False, "reason": "r", **table[l["text"]]} for l in lines if l["text"] in table]
        return LLMResponse(text=json.dumps({"decisions": decisions}))

    return s5.ModelCaller("m", call)


async def seeded_store(docs=None):
    store = FactStore()
    col = store.db.collection(FACTS)
    for doc in docs or [raw("f-car", CAR), raw("f-pipe", PIPE), raw("f-trial", TRIAL, reported_date=TRIAL_REPORTED), raw("f-maria", MARIA)]:
        await col.document(doc["id"]).set(doc)
    return store


async def classify_plan(store, out_dir):
    """A dry run over the store's facts; returns the plan file's path."""
    prepared = await s5.prepare(store.db, FACTS, owner_of)
    system, taxonomy, instruction = s5.load_prompt(*_prompt_texts())
    callers = {a.account_id: scripted(ANSWERS) for a in prepared.accounts}
    await s5.classify(prepared, callers, system, (taxonomy, instruction), collection=FACTS, out_dir=out_dir, now=NOW)
    return out_dir / f"fact_s5_classify_{NOW:%Y%m%dT%H%M%SZ}_plan.json"


def _prompt_texts():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    snapshot = (root / "prompts_snapshot" / "tokens" / "system" / "CONSOLIDATION_TAXONOMY.groovy").read_text(encoding="utf-8")
    instruction = (root / "scripts" / "migration" / "fact_s5_classify_instruction.txt").read_text(encoding="utf-8")
    return snapshot, instruction, snapshot


def outcomes_of(out_dir, now=NOW):
    path = out_dir / f"fact_s5_classify_{now:%Y%m%dT%H%M%SZ}_applied.json"
    return json.loads(path.read_text(encoding="utf-8"))["outcomes"]


# ---- what apply writes ----------------------------------------------------------------------------------------------

async def test_apply_writes_exactly_the_expiry_fields_and_one_trail_entry(tmp_path):
    store = await seeded_store()
    plan = await classify_plan(store, tmp_path)
    before = copy.deepcopy(store.docs)
    counts = await s5.apply_plan(store.repo, plan, FACTS, tmp_path, LATER)
    assert counts == {"applied": 4, "already_applied": 0, "skipped_changed": 0, "failed": 0, "not_current": 0}
    for fact_id, old in before.items():
        new = store.docs[fact_id]
        assert {k: v for k, v in new.items() if k not in NEW_KEYS} == {k: v for k, v in old.items() if k not in NEW_KEYS}
        assert set(new) - set(old) == NEW_KEYS                                  # only these four are added
        assert isinstance(new["expiry_kind"], str)                              # stored as its value, not an enum
        [event] = new["lifecycle_events"]
        assert (event["from"], event["to"], event["actor"], event["at"]) == ("current", "current", "migration", LATER)
        assert event["reason"] == "expiry classified at S5 (model)"
    assert store.docs["f-car"]["expiry_kind"] == "date" and store.docs["f-car"]["ttl_days"] is None
    assert store.docs["f-car"]["expires_at"] == datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc)
    assert store.docs["f-pipe"]["ttl_days"] == 30 and store.docs["f-pipe"]["expires_at"] == REPORTED + timedelta(days=30)
    assert store.docs["f-maria"]["expiry_kind"] == "none" and store.docs["f-maria"]["expires_at"] is None


async def test_applied_json_holds_ids_and_outcomes_and_no_text(tmp_path, capsys):
    store = await seeded_store()
    plan = await classify_plan(store, tmp_path)
    capsys.readouterr()
    await s5.apply_plan(store.repo, plan, FACTS, tmp_path, LATER)
    path = tmp_path / f"fact_s5_classify_{LATER:%Y%m%dT%H%M%SZ}_applied.json"
    text = path.read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["plan"] == plan.name and set(report["outcomes"]) == {"f-car", "f-pipe", "f-trial", "f-maria"}
    assert set(report["outcomes"].values()) == {"applied"}
    assert all(t not in text for t in (CAR, PIPE, TRIAL, MARIA))
    out = capsys.readouterr().out
    assert "applied: 4" in out and "f-car" not in out and CAR not in out


# ---- a fact that changed since the scan -----------------------------------------------------------------------------

async def test_a_fact_edited_superseded_or_deleted_after_the_scan_is_skipped(tmp_path):
    store = await seeded_store(
        [raw("f-edit", CAR), raw("f-sup", PIPE), raw("f-gone", TRIAL), raw("f-ok", MARIA), raw("f-successor", "the newer pipeline text")]
    )
    store.docs["f-successor"]["state"] = "current"
    plan = await classify_plan(store, tmp_path)
    col = store.db.collection(FACTS)
    await col.document("f-edit").update({"text": "edited"})
    await col.document("f-sup").update({"state": "superseded", "is_current": False, "superseded_by": "f-successor"})
    await col.document("f-gone").delete()
    counts = await s5.apply_plan(store.repo, plan, FACTS, tmp_path, LATER)
    outcomes = outcomes_of(tmp_path, LATER)
    assert outcomes["f-edit"] == outcomes["f-sup"] == outcomes["f-gone"] == "skipped_changed" and outcomes["f-ok"] == "applied"
    assert counts["skipped_changed"] == 3 and counts["applied"] == 2   # f-ok and the successor, which has its own entry
    assert "expiry_kind" not in store.docs["f-edit"] and store.docs["f-edit"]["text"] == "edited"
    assert "expiry_kind" not in store.docs["f-sup"] and outcomes["f-successor"] == "applied"
    changed = (tmp_path / f"fact_s5_classify_{LATER:%Y%m%dT%H%M%SZ}_changed.txt").read_text().split()
    assert sorted(changed) == ["f-edit", "f-gone", "f-sup"]


async def test_a_race_during_the_write_is_skipped_and_nothing_is_written(tmp_path):
    store = await seeded_store([raw("f-race", CAR), raw("f-ok", MARIA)])
    plan = await classify_plan(store, tmp_path)
    store.db.interfere_on_txn_read(FACTS, "f-race", {"text": "raced"})
    counts = await s5.apply_plan(store.repo, plan, FACTS, tmp_path, LATER)
    assert outcomes_of(tmp_path, LATER) == {"f-race": "skipped_changed", "f-ok": "applied"} and counts["skipped_changed"] == 1
    assert "expiry_kind" not in store.docs["f-race"] and store.docs["f-race"]["text"] == "raced"
    assert (tmp_path / f"fact_s5_classify_{LATER:%Y%m%dT%H%M%SZ}_changed.txt").read_text().split() == ["f-race"]


# ---- a second apply -------------------------------------------------------------------------------------------------

async def test_a_second_apply_of_the_same_plan_changes_nothing(tmp_path):
    store = await seeded_store()
    plan = await classify_plan(store, tmp_path)
    await s5.apply_plan(store.repo, plan, FACTS, tmp_path, LATER)
    after_first = copy.deepcopy(store.docs)
    again = LATER + timedelta(hours=1)
    counts = await s5.apply_plan(store.repo, plan, FACTS, tmp_path, again)
    assert counts == {"applied": 0, "already_applied": 4, "skipped_changed": 0, "failed": 0, "not_current": 0}
    assert store.docs == after_first
    assert set(outcomes_of(tmp_path, again).values()) == {"already_applied"}
    assert not (tmp_path / f"fact_s5_classify_{again:%Y%m%dT%H%M%SZ}_changed.txt").exists()


async def test_a_fact_the_janitor_staled_after_the_first_apply_is_skipped_on_a_rerun(tmp_path):
    store = await seeded_store([raw("f-trial", TRIAL, reported_date=TRIAL_REPORTED), raw("f-maria", MARIA)])
    plan = await classify_plan(store, tmp_path)
    await s5.apply_plan(store.repo, plan, FACTS, tmp_path, LATER)
    [due] = [d for d in await store.repo.find_due_facts(LATER) if d.fact_id == "f-trial"]
    await store.repo.stale_due_fact(due, stale_fields(due, LATER))
    assert store.docs["f-trial"]["state"] == "stale" and store.docs["f-trial"]["lifecycle_events"][-1]["actor"] == "janitor"
    again = LATER + timedelta(hours=1)
    counts = await s5.apply_plan(store.repo, plan, FACTS, tmp_path, again)
    assert outcomes_of(tmp_path, again) == {"f-trial": "skipped_changed", "f-maria": "already_applied"}
    assert counts["skipped_changed"] == 1
    assert store.docs["f-trial"]["state"] == "stale"


# ---- an unreadable document -----------------------------------------------------------------------------------------

async def test_an_unreadable_document_fails_alone_and_prints_only_the_error_type(tmp_path, capsys):
    store = await seeded_store([raw("f-a", CAR), raw("f-bad", PIPE, type="bogus-type-value"), raw("f-c", MARIA)])
    plan = await classify_plan(store, tmp_path)
    capsys.readouterr()
    counts = await s5.apply_plan(store.repo, plan, FACTS, tmp_path, LATER)
    assert counts == {"applied": 2, "already_applied": 0, "skipped_changed": 0, "failed": 1, "not_current": 0}
    assert outcomes_of(tmp_path, LATER) == {"f-a": "applied", "f-bad": "failed", "f-c": "applied"}
    out = capsys.readouterr().out
    assert "ValidationError" in out
    for leaked in ("bogus-type-value", PIPE, "f-bad", ACC):
        assert leaked not in out
    assert "expiry_kind" not in store.docs["f-bad"]


# ---- refusals before any read or write ------------------------------------------------------------------------------

class Untouchable:
    """A repository that must never be reached."""

    async def resolve_current(self, *args, **kwargs):
        raise AssertionError("a fact was read")

    async def update_fact_fields(self, *args, **kwargs):
        raise AssertionError("a fact was written")


async def test_a_malformed_foreign_or_outside_plan_is_refused_before_any_fact_is_touched(tmp_path, tmp_path_factory):
    store = await seeded_store()
    plan = await classify_plan(store, tmp_path)
    document = json.loads(plan.read_text(encoding="utf-8"))

    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({**document, "decisions": [{**document["decisions"][0], "token": ""}]}), encoding="utf-8")
    not_json = tmp_path / "not_json.json"
    not_json.write_text("{ nope", encoding="utf-8")
    for bad in (broken, not_json):
        with pytest.raises(PlanFileError):
            await s5.apply_plan(Untouchable(), bad, FACTS, tmp_path, LATER)
    with pytest.raises(PlanFileError):
        await s5.apply_plan(Untouchable(), plan, "another_collection", tmp_path, LATER)

    outside = tmp_path_factory.mktemp("elsewhere") / "plan.json"
    outside.write_text(plan.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(s3_dry_run_kit.PathOutsideMemory):
        await s5.apply_plan(Untouchable(), outside, FACTS, tmp_path, LATER)
    assert not list(tmp_path.glob("*_applied.json"))


async def test_a_late_bad_entry_refuses_the_whole_plan(tmp_path):
    store = await seeded_store()
    plan = await classify_plan(store, tmp_path)
    document = json.loads(plan.read_text(encoding="utf-8"))
    document["decisions"][-1]["fields"]["ttl_days"] = 7           # a stray ttl_days on a "none" entry
    broken = tmp_path / "late.json"
    broken.write_text(json.dumps(document), encoding="utf-8")
    before = copy.deepcopy(store.docs)
    with pytest.raises(PlanFileError, match="plan entry 3"):
        await s5.apply_plan(store.repo, broken, FACTS, tmp_path, LATER)
    assert store.docs == before


# ---- the janitor reads what classify wrote --------------------------------------------------------------------------

async def test_the_janitor_finds_exactly_the_facts_the_report_marked_due(tmp_path):
    store = await seeded_store()
    plan = await classify_plan(store, tmp_path)
    report = (tmp_path / f"fact_s5_classify_{NOW:%Y%m%dT%H%M%SZ}_report.md").read_text(encoding="utf-8")
    due_texts = {text for text in (CAR, PIPE, TRIAL, MARIA) for row in report.split("\n") if text in row and "| yes |" in row}
    assert due_texts == {TRIAL}
    await s5.apply_plan(store.repo, plan, FACTS, tmp_path, NOW)
    due = await store.repo.find_due_facts(NOW)
    assert [(d.fact_id, d.expiry_kind.value) for d in due] == [("f-trial", "date")]
    later = await store.repo.find_due_facts(datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert {(d.fact_id, d.expiry_kind.value) for d in later} == {("f-trial", "date"), ("f-car", "date"), ("f-pipe", "horizon")}


# ---- main --apply ---------------------------------------------------------------------------------------------------

def _apply_main_world(monkeypatch, tmp_path, docs=None):
    import asyncio
    from types import SimpleNamespace

    import scripts._firestore as firestore_module
    from tests.support.fact_store import make_env_config

    async def prepare_all():
        store = await seeded_store(docs)
        return store, await classify_plan(store, tmp_path)

    store, plan = asyncio.run(prepare_all())
    env = make_env_config()
    env.domain_prompt_tokens_collection = "tokens"
    monkeypatch.setattr(s5, "load_env", lambda: None)
    monkeypatch.setattr(s5, "EnvironmentConfig", lambda: SimpleNamespace(**vars(env)))
    monkeypatch.setattr(s5, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(firestore_module, "async_client", lambda: store.db)
    return store, plan


def test_main_apply_writes_the_plan_and_exits_zero(monkeypatch, tmp_path, capsys):
    store, plan = _apply_main_world(monkeypatch, tmp_path)
    assert s5.main(["--apply", str(plan)]) == 0
    assert "applied: 4" in capsys.readouterr().out
    assert {doc["expiry_kind"] for doc in store.docs.values()} == {"date", "horizon", "none"}
    assert len(list(tmp_path.glob("*_applied.json"))) == 1


def test_main_apply_exits_one_when_an_entry_failed(monkeypatch, tmp_path, capsys):
    store, plan = _apply_main_world(monkeypatch, tmp_path, [raw("f-a", CAR), raw("f-bad", PIPE, type="bogus")])
    assert s5.main(["--apply", str(plan)]) == 1
    out = capsys.readouterr().out
    assert "failed: 1" in out and "applied: 1" in out


# ---- closed facts -----------------------------------------------------------------------------------------------------

async def classify_closed_plan(store, out_dir, ids):
    prepared = await s5.prepare(store.db, FACTS, owner_of, ids, include_closed=True)
    system, taxonomy, instruction = s5.load_prompt(*_prompt_texts())
    callers = {a.account_id: scripted(ANSWERS) for a in prepared.accounts}
    await s5.classify(prepared, callers, system, (taxonomy, instruction), collection=FACTS, out_dir=out_dir, now=NOW)
    return out_dir / f"fact_s5_classify_{NOW:%Y%m%dT%H%M%SZ}_plan.json"


async def test_apply_refuses_the_entry_of_a_closed_fact_and_writes_the_current_one(tmp_path):
    store = await seeded_store([
        raw("f-arch", CAR, state="archived", is_current=False), raw("f-inv", PIPE, state="invalidated", is_current=False),
        raw("f-cur", MARIA),
    ])
    plan = await classify_closed_plan(store, tmp_path, {"f-arch", "f-inv", "f-cur"})
    assert {e["fact_id"]: e["state"] for e in json.loads(plan.read_text())["decisions"]} == {
        "f-arch": "archived", "f-inv": "invalidated", "f-cur": "current",
    }
    before = copy.deepcopy(store.docs)
    counts = await s5.apply_plan(store.repo, plan, FACTS, tmp_path, LATER)
    assert counts == {"applied": 1, "already_applied": 0, "skipped_changed": 0, "failed": 0, "not_current": 2}
    assert outcomes_of(tmp_path, LATER) == {"f-arch": "not_current", "f-inv": "not_current", "f-cur": "applied"}
    assert store.docs["f-arch"] == before["f-arch"] and store.docs["f-inv"] == before["f-inv"]
    assert store.docs["f-cur"]["expiry_kind"] == "none"
