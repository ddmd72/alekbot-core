"""scripts/migration/fact_s5_classify.py: scan, prompt check, estimate, cap, the dry run and the command line (no model, no database)."""
import ast
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from google.cloud.firestore_v1.vector import Vector

from scripts.consolidation import s3_dry_run_kit
from scripts.consolidation.s3_dry_run_kit import CostMeter
from scripts.migration import fact_s5_classify as s5
from scripts.migration.fact_s5_classify_rules import RESPONSE_SCHEMA
from src.domain.llm import LLMResponse, UsageMetadata
from src.domain.owner_timezone import ResolvedOwnerZone
from src.infrastructure.agent_config import CONSOLIDATION
from tests.support.clock_instants import BERLIN
from tests.support.firestore_eval import EvalDb

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_TEXT = (ROOT / "prompts_snapshot" / "tokens" / "system" / "CONSOLIDATION_TAXONOMY.groovy").read_text(encoding="utf-8")
INSTRUCTION_TEXT = (ROOT / "scripts" / "migration" / "fact_s5_classify_instruction.txt").read_text(encoding="utf-8")
FACTS = "eval_domain_facts"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
REPORTED = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
TRIAL_REPORTED = datetime(2026, 4, 1, 9, 30, tzinfo=timezone.utc)
OWNER_ACC = "acc-owner-0001"
OTHER_ACC = "acc-other-0002"
ORPHAN_ACC = "acc-orphan-0003"
MODEL = "claude-sonnet-5-5"

CAR = "car service Nov 4, bring the logbook"
CONF = "a conference Nov 18-20"
PIPE = "refactoring the doc pipeline"
GUITAR = "practises guitar 1 h/day"
TRIAL = "a cloud trial ended May 20"
MARIA = "mother is Maria"
RULE = "never give partial answers"
SYSTEM_TEXT = "system-owned fact text"
DECIDED_TEXT = "an already decided fact text"
ORPHAN_TEXT = "orphan account fact text"

ANSWERS = {
    CAR: {"kind": "date", "date": "2026-11-04"},
    CONF: {"kind": "date", "date": "2026-11-20"},
    PIPE: {"kind": "horizon", "days": 30},
    GUITAR: {"kind": "horizon", "days": 90},
    TRIAL: {"kind": "date", "date": "2026-05-20"},
    MARIA: {"kind": "none"},
}


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_dry_run_kit, "MEMORY_DIR", tmp_path)
    return tmp_path


def raw(doc_id, text, account=OWNER_ACC, **over):
    doc = {
        "id": doc_id, "account_id": account, "created_by_user_id": "usr-1", "lineage_id": f"L-{doc_id}",
        "text": text, "type": "event", "domain": "possession", "temporal_class": "dynamic", "tags": ["t"],
        "metadata": {"k": "v"}, "state": "current", "is_current": True, "valid_to": None,
        "vector": Vector([0.1]), "tags_vector": Vector([0.2]), "metadata_vector": Vector([0.3]),
        "created_at": REPORTED, "reported_date": REPORTED, "valid_from": REPORTED, "last_updated": REPORTED,
    }
    doc.update(over)
    return doc


async def seed(db, docs):
    col = db.collection(FACTS)
    for doc in docs:
        await col.document(doc["id"]).set(doc)


def standard_docs():
    return [
        raw("f-car", CAR), raw("f-conf", CONF), raw("f-pipe", PIPE), raw("f-guitar", GUITAR), raw("f-trial", TRIAL, reported_date=TRIAL_REPORTED),
        raw("f-maria", MARIA), raw("f-rule", RULE, domain="AGENT_DIRECTIVE"),
        raw("f-system", SYSTEM_TEXT, account="SYSTEM"), raw("f-decided", DECIDED_TEXT, expiry_kind="none"),
        raw("f-orphan", ORPHAN_TEXT, account=ORPHAN_ACC), raw("f-noacc", "no account text", account=""),
    ]


async def owner_of(account_id):
    if account_id == ORPHAN_ACC:
        return ResolvedOwnerZone(None, "UTC")
    return ResolvedOwnerZone("owner-1", BERLIN)


def scripted(table, requests=None, model=MODEL, raise_on=(), raise_times=2, usage=None, served_by=None):
    """A semantic fake of the model: reads the request's JSON lines and answers each `n` from `table` (keyed by text)."""
    state = {"raised": 0}

    async def call(request):
        if requests is not None:
            requests.append(request)
        lines = [json.loads(line) for line in request.messages[0].parts[0].text.split("\n") if line.startswith("{")]
        if any(line["text"] in raise_on for line in lines) and state["raised"] < raise_times:
            state["raised"] += 1
            raise RuntimeError("provider down")
        decisions = []
        for line in lines:
            entry = table.get(line["text"])
            if entry is not None:
                decisions.append({"unsure": False, "reason": "row", **entry, "n": line["n"]})
        return LLMResponse(text=json.dumps({"decisions": decisions}), usage_metadata=usage, served_by_fallback=served_by)

    return s5.ModelCaller(model, call)


def prompt():
    return s5.load_prompt(SNAPSHOT_TEXT, INSTRUCTION_TEXT, SNAPSHOT_TEXT)


async def run(db, caller, out_dir, ids=None, meter=None, owner=owner_of, now=NOW):
    prepared = await s5.prepare(db, FACTS, owner, ids)
    callers = {a.account_id: caller for a in prepared.accounts}
    system, taxonomy, instruction = prompt()
    result = await s5.classify(
        prepared, callers, system, (taxonomy, instruction), collection=FACTS, out_dir=out_dir, now=now, meter=meter,
    )
    return result, prepared


def plan_of(out_dir, now=NOW):
    return json.loads((out_dir / f"fact_s5_classify_{now:%Y%m%dT%H%M%SZ}_plan.json").read_text(encoding="utf-8"))


def by_fact(plan):
    return {d["fact_id"]: d for d in plan["decisions"]}


async def standard_db():
    db = EvalDb()
    await seed(db, standard_docs())
    return db


# ---- the dry run ----------------------------------------------------------------------------------------------------

async def test_the_dry_run_leaves_the_collection_unchanged(tmp_path):
    db = await standard_db()
    col = db.collection(FACTS)
    before = (copy.deepcopy(col.docs), copy.deepcopy(col.versions))
    await run(db, scripted(ANSWERS), tmp_path)
    assert (col.docs, col.versions) == before


async def test_the_plan_holds_the_tokens_the_fields_and_the_skips(tmp_path):
    db = await standard_db()
    col = db.collection(FACTS)
    result, _ = await run(db, scripted(ANSWERS), tmp_path)
    plan = plan_of(tmp_path)
    decisions = by_fact(plan)
    assert plan["kind"] == "fact_s5_classify_plan" and plan["collection"] == FACTS and plan["stopped"] is None
    assert plan["models"] == {OWNER_ACC: MODEL}
    assert set(decisions) == {"f-car", "f-conf", "f-pipe", "f-guitar", "f-trial", "f-maria", "f-rule"}
    for fact_id, entry in decisions.items():
        assert entry["token"] == str(col.versions[fact_id]) and entry["account_id"] == OWNER_ACC
    car = decisions["f-car"]
    assert car["source"] == "model" and car["fields"] == {
        "expiry_kind": "date", "ttl_days": None, "expires_at": "2026-11-04T23:00:00+00:00",
    }
    assert decisions["f-pipe"]["fields"] == {
        "expiry_kind": "horizon", "ttl_days": 30, "expires_at": (REPORTED + timedelta(days=30)).isoformat(),
    }
    assert decisions["f-maria"]["fields"] == {"expiry_kind": "none", "ttl_days": None, "expires_at": None}
    assert decisions["f-rule"]["source"] == "pinned" and decisions["f-rule"]["fields"]["expiry_kind"] == "none"
    assert decisions["f-trial"]["fields"]["expires_at"] == "2026-05-20T22:00:00+00:00"   # long past: kept, not refused
    skipped = {s["fact_id"]: s["reason"] for s in plan["skipped"]}
    assert skipped == {
        "f-system": "system_account", "f-decided": "already_decided", "f-orphan": "owner_unresolved", "f-noacc": "no_account",
    }
    assert plan["not_decided"] == [] and len(plan["prompt"]["taxonomy_sha256"]) == 64
    assert result["decided"] == 7 and result["due_now"] == 1 and result["stopped"] is None


async def test_every_request_is_built_as_specified_and_hides_ids(tmp_path):
    db = await standard_db()
    requests = []
    await run(db, scripted(ANSWERS, requests), tmp_path)
    assert requests
    asked = "\n".join(r.messages[0].parts[0].text for r in requests)
    assert RULE not in asked  # the pinned fact is never sent
    for request in requests:
        assert request.model_name == MODEL
        assert request.response_schema is RESPONSE_SCHEMA
        assert request.response_mime_type == "application/json"
        assert request.thinking == CONSOLIDATION.thinking_effort and request.max_tokens == CONSOLIDATION.max_tokens
        assert request.timeout == 500
        assert "kinds_table" in request.system_instruction and "classify_stored_facts" in request.system_instruction
        text = request.messages[0].parts[0].text
        for secret in ("f-car", "f-pipe", OWNER_ACC, "usr-1", "L-f-car"):
            assert secret not in text and secret not in request.system_instruction


async def test_a_missing_decision_is_asked_once_more_then_falls_back(tmp_path):
    db = await standard_db()
    requests = []
    table = {k: v for k, v in ANSWERS.items() if k != CONF}   # the model never answers the conference fact
    await run(db, scripted(table, requests), tmp_path)
    assert len(requests) == 2
    retry = requests[1].messages[0].parts[0].text.split("\n")
    n = next(i for i, line in enumerate(requests[0].messages[0].parts[0].text.split("\n")[1:], 1) if CONF in line)
    assert retry[0] == f"Your previous answer gave no usable decision for these facts: n={n} (missing). Decide each of them again."
    assert len(retry) == 3 and CONF in retry[2]
    entry = by_fact(plan_of(tmp_path))["f-conf"]
    assert entry["source"] == "fallback" and entry["code"] == "missing" and entry["fields"]["expiry_kind"] == "none"


async def test_a_retry_that_answers_is_used(tmp_path):
    db = await standard_db()
    calls = []
    first_miss = {"done": False}

    async def call(request):
        text = request.messages[0].parts[0].text
        calls.append(text)
        lines = [json.loads(line) for line in text.split("\n") if line.startswith("{")]
        out = []
        for line in lines:
            if line["text"] == CAR and not first_miss["done"]:
                first_miss["done"] = True
                continue
            out.append({"n": line["n"], **ANSWERS[line["text"]], "unsure": False, "reason": "r"} if line["text"] in ANSWERS else
                       {"n": line["n"], "kind": "none", "unsure": False, "reason": "r"})
        return LLMResponse(text=json.dumps({"decisions": out}))

    await run(db, s5.ModelCaller(MODEL, call), tmp_path)
    assert len(calls) == 2
    assert by_fact(plan_of(tmp_path))["f-car"]["source"] == "model"


async def test_unsure_is_none_and_the_report_says_what_the_model_said(tmp_path):
    db = await standard_db()
    table = {**ANSWERS, CAR: {"kind": "date", "date": "2026-11-04", "unsure": True, "reason": "no real date"}}
    await run(db, scripted(table), tmp_path)
    entry = by_fact(plan_of(tmp_path))["f-car"]
    assert entry["source"] == "unsure" and entry["fields"]["expiry_kind"] == "none"
    report = (tmp_path / "fact_s5_classify_20261027T120000Z_report.md").read_text(encoding="utf-8")
    row = next(line for line in report.split("\n") if CAR in line)
    assert "unsure (model said date)" in row and "no real date" in row


async def test_a_date_before_the_reported_day_becomes_none_with_its_flag(tmp_path):
    db = await standard_db()
    table = {**ANSWERS, CAR: {"kind": "date", "date": "2026-09-20", "reason": "already over"}}   # reported 2026-10-01
    result, _ = await run(db, scripted(table), tmp_path)
    entry = by_fact(plan_of(tmp_path))["f-car"]
    assert entry["source"] == "past_at_report" and entry["fields"] == {"expiry_kind": "none", "ttl_days": None, "expires_at": None}
    report = (tmp_path / "fact_s5_classify_20261027T120000Z_report.md").read_text(encoding="utf-8")
    row = next(line for line in report.split("\n") if CAR in line)
    assert "past at report (model said date 2026-09-20)" in row
    assert result["by_source"]["past_at_report"] == 1


async def test_unsure_on_a_horizon_type_fact_is_flagged_and_none(tmp_path):
    db = await standard_db()
    table = {**ANSWERS, PIPE: {"kind": "horizon", "days": 30, "unsure": True, "reason": "not sure it is still active"}}
    await run(db, scripted(table), tmp_path)
    entry = by_fact(plan_of(tmp_path))["f-pipe"]
    assert entry["source"] == "unsure" and entry["fields"] == {"expiry_kind": "none", "ttl_days": None, "expires_at": None}
    report = (tmp_path / "fact_s5_classify_20261027T120000Z_report.md").read_text(encoding="utf-8")
    row = next(line for line in report.split("\n") if PIPE in line)
    assert "| none |" in row and "unsure (model said horizon)" in row


async def test_an_iso_string_reported_date_cannot_carry_a_horizon_and_a_directive_is_never_sent(tmp_path):
    db = EvalDb()
    await seed(db, [raw("f-str", PIPE, reported_date="2026-10-01T09:30:00Z"), raw("f-dir", RULE, domain="AGENT_DIRECTIVE")])
    requests = []
    result, _ = await run(db, scripted(ANSWERS, requests), tmp_path)
    entry = by_fact(plan_of(tmp_path))
    assert entry["f-str"]["source"] == "fallback" and entry["f-str"]["code"] == "no_reported_date"
    assert entry["f-dir"]["source"] == "pinned"
    assert result["no_reported_date"] == 1 and result["fallback_by_code"] == {"no_reported_date": 1}
    assert all(RULE not in r.messages[0].parts[0].text for r in requests)


async def test_filtered_mode_takes_only_the_listed_facts(tmp_path):
    db = await standard_db()
    await run(db, scripted(ANSWERS), tmp_path, ids={"f-car", "f-decided", "f-ghost"})
    plan = plan_of(tmp_path)
    assert list(by_fact(plan)) == ["f-car"]
    assert {s["fact_id"]: s["reason"] for s in plan["skipped"]} == {
        "f-decided": "already_decided", "f-ghost": "not_found_or_not_current",
    }


# ---- stopping -------------------------------------------------------------------------------------------------------

def six_facts():
    return [raw(f"g{i}", f"fact number {i}") for i in range(1, 7)]


def six_answers():
    return {f"fact number {i}": {"kind": "none"} for i in range(1, 7)}


async def test_a_call_that_fails_twice_stops_the_run_and_lists_the_undecided(tmp_path, monkeypatch):
    monkeypatch.setattr(s5, "BATCH_SIZE", 2)
    db = EvalDb()
    await seed(db, six_facts() + [raw("g-rule", RULE, domain="AGENT_DIRECTIVE")])
    result, _ = await run(db, scripted(six_answers(), raise_on={"fact number 3"}), tmp_path)
    assert result["stopped"] == "call failed twice: RuntimeError"
    plan = plan_of(tmp_path)
    assert plan["stopped"] == "call failed twice: RuntimeError"
    assert set(by_fact(plan)) == {"g1", "g2", "g-rule"}                      # batch 1 and the pinned fact are kept
    ids = lambda name: (tmp_path / f"fact_s5_classify_20261027T120000Z_{name}.txt").read_text().split()
    assert ids("not_decided") == ["g5", "g6", "g3", "g4"]                    # later batches first, the failing batch last
    assert ids("stopped_batch") == ["g3", "g4"]
    assert plan["not_decided"] == ["g5", "g6", "g3", "g4"]


async def test_a_call_that_fails_once_is_repeated_without_a_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(s5, "BATCH_SIZE", 2)
    db = EvalDb()
    await seed(db, six_facts())
    result, _ = await run(db, scripted(six_answers(), raise_on={"fact number 3"}, raise_times=1), tmp_path)
    assert result["stopped"] is None and result["decided"] == 6
    assert not list(tmp_path.glob("*_not_decided.txt")) and not list(tmp_path.glob("*_stopped_batch.txt"))


@pytest.mark.parametrize("name", ["gemini", ""])
async def test_an_answer_from_the_fallback_provider_stops_the_run_and_is_not_used(tmp_path, monkeypatch, name):
    monkeypatch.setattr(s5, "BATCH_SIZE", 2)
    db = EvalDb()
    await seed(db, six_facts())
    result, _ = await run(db, scripted(six_answers(), served_by=name), tmp_path)
    assert result["stopped"] == f"answered by the fallback provider {name}"
    assert by_fact(plan_of(tmp_path)) == {}
    assert (tmp_path / "fact_s5_classify_20261027T120000Z_not_decided.txt").read_text().split() == ["g3", "g4", "g5", "g6", "g1", "g2"]


class Standin:
    """What `_call_llm` is on the live agent, reduced to its answer and its usage."""

    def __init__(self, usage):
        self.model_name = MODEL
        self.usage = usage
        self.calls = 0

    async def _call_llm(self, request):
        self.calls += 1
        lines = [json.loads(line) for line in request.messages[0].parts[0].text.split("\n") if line.startswith("{")]
        decisions = [{"n": line["n"], "kind": "none", "unsure": False, "reason": "r"} for line in lines]
        return LLMResponse(text=json.dumps({"decisions": decisions}), usage_metadata=self.usage)


async def test_the_spend_cap_stops_the_run_before_the_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr(s5, "BATCH_SIZE", 2)
    db = EvalDb()
    await seed(db, six_facts()[:4])
    agent = Standin(UsageMetadata(prompt_tokens=10_000, completion_tokens=1_000))
    meter = CostMeter(tmp_path / "ledger.json", budget_usd=0.0001)
    try:
        s5.wrap_once(meter, [agent])
        caller = s5.model_caller(agent, "owner-1", OWNER_ACC)
        result, _ = await run(db, caller, tmp_path, meter=meter)
        assert result["stopped"] == "spend cap" and agent.calls == 1
        assert result["cost"]["run_usd"] > 0 and result["cost"]["spent_usd"] == result["cost"]["run_usd"]
        assert (tmp_path / "fact_s5_classify_20261027T120000Z_not_decided.txt").read_text().split() == ["g3", "g4"]
    finally:
        meter.close()


async def test_an_unpriced_model_is_refused_by_the_meter_as_a_stop(tmp_path):
    db = EvalDb()
    await seed(db, six_facts()[:2])
    agent = Standin(None)
    agent.model_name = "no-such-model"
    meter = CostMeter(tmp_path / "ledger.json", budget_usd=5)
    try:
        s5.wrap_once(meter, [agent])
        result, _ = await run(db, s5.model_caller(agent, "owner-1", OWNER_ACC), tmp_path, meter=meter)
        assert result["stopped"] == "spend cap" and agent.calls == 0
    finally:
        meter.close()


# ---- the cap, the estimate and the meter ----------------------------------------------------------------------------

def test_the_cap_covers_all_runs(tmp_path):
    ledger = tmp_path / "classify_spend.json"
    first = CostMeter(ledger, budget_usd=10)
    first.record("claude-sonnet-5-5", LLMResponse(text="", usage_metadata=UsageMetadata(prompt_tokens=1_000_000)))
    first.close()
    second = CostMeter(ledger, budget_usd=3.0)
    try:
        assert second.spent_usd == pytest.approx(2.0)
        refuse, _ = s5.cap_verdict(second.spent_usd, (1.5, 3.0), 3.0)
        assert refuse is True
    finally:
        second.close()
    refuse, warning = s5.cap_verdict(1.0, (1.5, 3.0), 3.0)
    assert refuse is False and warning is not None and "may pass the cap" in warning
    assert s5.cap_verdict(0.0, (1.0, 2.0), 3.0) == (False, None)


async def test_estimate_lines_show_the_model_the_zone_the_total_and_the_spend(tmp_path):
    db = await standard_db()
    prepared = await s5.prepare(db, FACTS, owner_of)
    system, _, _ = prompt()
    lines, total = s5.estimate_lines(prepared, {OWNER_ACC: MODEL}, system, spent_usd=2.0)
    assert len(prepared.accounts) == 1 and total is not None and 0 < total[0] <= total[1]
    assert lines[0].startswith("account acc-owne: model claude-sonnet-5-5, owner zone Europe/Berlin, 6 facts, 1 batches, $")
    assert lines[-2].startswith("total: $") and lines[-1] == "spent so far (all classify runs): $2.0000"
    lines, total = s5.estimate_lines(prepared, {OWNER_ACC: "no-such-model"}, system)
    assert total is None and "unpriced" in lines[0] and lines[-1] == "total: unpriced"


async def test_estimate_and_summary_show_a_utc_zone(tmp_path):
    db = EvalDb()
    await seed(db, [raw("u1", "fact in a utc account", account=OTHER_ACC)])

    async def utc_owner(account_id):
        return ResolvedOwnerZone("owner-2", "UTC")

    prepared = await s5.prepare(db, FACTS, utc_owner)
    system, _, _ = prompt()
    lines, _ = s5.estimate_lines(prepared, {OTHER_ACC: MODEL}, system)
    assert "owner zone UTC" in lines[0]
    result, _ = await run(db, scripted({"fact in a utc account": {"kind": "none"}}), tmp_path, owner=utc_owner)
    report = (tmp_path / "fact_s5_classify_20261027T120000Z_report.md").read_text(encoding="utf-8")
    assert "zone acc-othe: UTC" in report
    summary_part = [line for line in report.split("\n") if line.startswith("- ")]
    assert summary_part[-len(s5.RESIDUALS):] == [f"- {line}" for line in s5.RESIDUALS]
    assert result["zones"] == {"acc-othe": "UTC"}


class Metered:
    def __init__(self):
        self.model_name = MODEL
        self.calls = 0

    async def _call_llm(self, request):
        self.calls += 1
        return LLMResponse(text="{}", usage_metadata=UsageMetadata(prompt_tokens=100, completion_tokens=10))


async def test_wrap_once_meters_each_agent_once_and_a_caller_built_earlier_is_still_metered(tmp_path):
    shared, other = Metered(), Metered()
    meter = CostMeter(tmp_path / "ledger.json", budget_usd=5)
    try:
        callers = [s5.model_caller(shared, "o1", "acc-1"), s5.model_caller(shared, "o1", "acc-2"), s5.model_caller(other, "o2", "acc-3")]
        s5.wrap_once(meter, [shared, shared, other])   # built before the wrap: the call still goes through the meter
        for caller in callers:
            await caller.call(s5._request(caller, "sys", [{"n": 1}], None))
        assert (shared.calls, other.calls) == (2, 1)
        assert meter.calls == 3
        assert json.loads((tmp_path / "ledger.json").read_text())["calls"] == 3
    finally:
        meter.close()


def test_check_models():
    s5.check_models({"acc-1234567890": MODEL}, MODEL)
    with pytest.raises(s5.ModelMismatch, match="acc-1234: claude-sonnet-5-5 .*claude-opus-5-5"):
        s5.check_models({"acc-1234567890": MODEL, "acc-9999999999": "claude-opus-5-5"}, "claude-opus-5-5")


# ---- stdout and files -----------------------------------------------------------------------------------------------

async def test_stdout_has_counts_and_paths_only_while_the_report_has_the_text(tmp_path, capsys):
    db = await standard_db()
    await run(db, scripted(ANSWERS), tmp_path)
    out = capsys.readouterr().out
    for secret in (CAR, CONF, PIPE, MARIA, RULE, "f-car", "f-maria", OWNER_ACC, ORPHAN_ACC, "usr-1"):
        assert secret not in out
    assert "decided: 7" in out and "zone acc-owne: Europe/Berlin" in out and "file: " in out
    report = (tmp_path / "fact_s5_classify_20261027T120000Z_report.md").read_text(encoding="utf-8")
    assert CAR in report and MARIA in report and "| yes |" in report
    assert "f-car" not in report


# ---- the prompt -----------------------------------------------------------------------------------------------------

def test_load_prompt_accepts_the_snapshot_and_names_both_parts():
    system, taxonomy, instruction = prompt()
    assert "kinds_table" in system and "classify_stored_facts" in system and "conflict_resolution" not in system
    assert len(taxonomy) == 64 and len(instruction) == 64 and taxonomy != instruction


def test_load_prompt_refuses_a_live_file_one_character_away():
    with pytest.raises(s5.PromptDrift, match="snapshot_pull --check"):
        s5.load_prompt(SNAPSHOT_TEXT, INSTRUCTION_TEXT, SNAPSHOT_TEXT[:-1] + "x")
    with pytest.raises(s5.PromptDrift):
        s5.load_prompt(SNAPSHOT_TEXT, INSTRUCTION_TEXT, SNAPSHOT_TEXT + "\n")


def test_the_snapshot_text_round_trips_through_the_serializer():
    serializer = s3_dry_run_kit._serializer()
    assert serializer.token_to_file(serializer.token_from_file(SNAPSHOT_TEXT)) == SNAPSHOT_TEXT


async def test_the_live_taxonomy_file_is_rendered_as_the_pull_renders_it():
    serializer = s3_dry_run_kit._serializer()
    token = serializer.token_from_file(SNAPSHOT_TEXT)
    db = EvalDb()
    await db.collection("tokens_system").document(s5.TAXONOMY_TOKEN_ID).set({**token, "updated_at": NOW, "created_at": NOW})
    assert await s5.live_taxonomy_file(db, "tokens_system") == SNAPSHOT_TEXT
    with pytest.raises(s5.PromptDrift, match="does not exist"):
        await s5.live_taxonomy_file(EvalDb(), "tokens_system")


# ---- the code of the module -----------------------------------------------------------------------------------------

FORBIDDEN_CALLS = {"set", "create", "delete", "batch", "commit", "add", "update"}


def test_the_module_writes_only_through_the_repository_port():
    tree = ast.parse((ROOT / "scripts" / "migration" / "fact_s5_classify.py").read_text(encoding="utf-8"))
    attribute_calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert attribute_calls & FORBIDDEN_CALLS == set()
    assert "update_fact_fields" in attribute_calls
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("snapshot_upload" in name for name in imported)


def test_the_docstring_is_the_help_and_names_the_rules():
    doc = s5.__doc__
    for needle in ("--expect-model", "--budget-usd", "--ids-file", "--apply", "ALL classify runs", "DEBUG_PROMPTS", "apply only on his word"):
        assert needle in doc
    flat = " ".join(doc.split())
    assert all(" ".join(line.split()) in flat for line in s5.RESIDUALS)
    assert s5.build_parser().description == doc


# ---- main -----------------------------------------------------------------------------------------------------------

class Reached(Exception):
    pass


class FakeEnvConfig:
    created_after = []
    ran = []

    def __init__(self):
        FakeEnvConfig.created_after.append(bool(FakeEnvConfig.ran))
        self.domain_facts_collection = FACTS
        self.domain_prompt_tokens_collection = "tokens"


@pytest.fixture
def cli(monkeypatch, tmp_path):
    FakeEnvConfig.created_after, FakeEnvConfig.ran = [], []
    reached = {"build_agent": [], "client": 0}

    def load_env():
        FakeEnvConfig.ran.append(True)
        monkeypatch.setenv("DEV_USER_ID", "dotenv-user")

    async def build_agent(user_id):
        reached["build_agent"].append(user_id)
        raise Reached()

    def async_client():
        reached["client"] += 1
        raise Reached()

    import scripts._firestore as firestore_module

    monkeypatch.delenv("DEV_USER_ID", raising=False)
    monkeypatch.setattr(s5, "load_env", load_env)
    monkeypatch.setattr(s5, "build_agent", build_agent)
    monkeypatch.setattr(s5, "EnvironmentConfig", FakeEnvConfig)
    monkeypatch.setattr(firestore_module, "async_client", async_client)
    return reached


def test_the_user_id_default_is_read_after_the_dotenv_file(cli):
    with pytest.raises(Reached):
        s5.main(["--expect-model", "m"])
    assert cli["build_agent"] == ["dotenv-user"] and FakeEnvConfig.created_after == [True]
    with pytest.raises(Reached):
        s5.main(["--expect-model", "m", "--user-id", "explicit"])
    assert cli["build_agent"] == ["dotenv-user", "explicit"]


def test_no_user_id_anywhere_stops_before_the_agent(cli, monkeypatch):
    monkeypatch.setattr(s5, "load_env", lambda: FakeEnvConfig.ran.append(True))
    with pytest.raises(SystemExit):
        s5.main(["--expect-model", "m"])
    assert cli["build_agent"] == [] and FakeEnvConfig.created_after == []


def test_a_dry_run_without_the_expected_model_stops_first(cli):
    with pytest.raises(SystemExit):
        s5.main([])
    assert cli["build_agent"] == [] and FakeEnvConfig.created_after == []


@pytest.mark.parametrize("extra", [["--budget-usd", "1"], ["--expect-model", "m"], ["--ids-file", "ids.txt"]])
def test_apply_takes_no_dry_run_flag(cli, extra):
    with pytest.raises(SystemExit):
        s5.main(["--apply", "x", *extra])
    assert cli["client"] == 0 and cli["build_agent"] == [] and FakeEnvConfig.created_after == []


def test_apply_builds_its_client_after_the_dotenv_file_and_the_config(cli):
    with pytest.raises(Reached):
        s5.main(["--apply", "plan.json"])
    assert cli["client"] == 1 and cli["build_agent"] == [] and FakeEnvConfig.created_after == [True]


def test_an_ids_file_outside_the_memory_directory_is_refused(cli, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "ids.txt"
    outside.write_text("a\n")
    with pytest.raises(s3_dry_run_kit.PathOutsideMemory):
        s5.main(["--expect-model", "m", "--ids-file", str(outside)])
    assert cli["build_agent"] == []


def test_the_ids_file_ignores_blank_and_comment_lines(memory_dir):
    path = memory_dir / "ids.txt"
    path.write_text("# a comment\nf1\n\n  f2  \n#f3\n", encoding="utf-8")
    assert s5._read_ids(str(path)) == {"f1", "f2"}


# ---- main: the dry run end to end on fakes ----------------------------------------------------------------------------

class Handles:
    def __init__(self, db, agent):
        self.db, self.agent = db, agent
        self.account_repo = self.user_repo = None


class Failing(Standin):
    async def _call_llm(self, request):
        self.calls += 1
        raise RuntimeError("provider down")


class World:
    def __init__(self, db, agents):
        self.db, self.agents = db, agents
        self.built = []


def build_world(monkeypatch, tmp_path, agents=None, drift=False):
    """The dry run's collaborators replaced by fakes: the database, the agents, the owner lookup, the token and the files."""
    import asyncio

    token = s3_dry_run_kit._serializer().token_from_file(SNAPSHOT_TEXT)
    if drift:
        token = {**token, "content": token["content"] + " drift"}
    db = EvalDb()

    async def seed_all():
        await seed(db, [raw("f-car", CAR), raw("f-maria", MARIA), raw("f-other", PIPE, account=OTHER_ACC)])
        await db.collection("tokens_system").document(s5.TAXONOMY_TOKEN_ID).set({**token, "updated_at": NOW})

    asyncio.run(seed_all())
    agents = agents or {
        "dev-user": Standin(UsageMetadata(prompt_tokens=1000, completion_tokens=100)),
        "owner-2": Standin(UsageMetadata(prompt_tokens=1000, completion_tokens=100)),
    }
    world = World(db, agents)

    async def build_agent(user_id):
        world.built.append(user_id)
        return Handles(db, agents[user_id])

    class Resolver:
        def __init__(self, account_repo, user_repo):
            pass

        async def resolve(self, account_id):
            return ResolvedOwnerZone("dev-user" if account_id == OWNER_ACC else "owner-2", BERLIN)

    class Env:
        domain_facts_collection = FACTS
        domain_prompt_tokens_collection = "tokens"

    monkeypatch.delenv("DEV_USER_ID", raising=False)
    monkeypatch.setattr(s5, "load_env", lambda: None)
    monkeypatch.setattr(s5, "build_agent", build_agent)
    monkeypatch.setattr(s5, "OwnerTimezoneResolver", Resolver)
    monkeypatch.setattr(s5, "EnvironmentConfig", Env)
    monkeypatch.setattr(s5, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(s5, "SPEND_LEDGER", tmp_path / "fact_s5_classify_spend.json")
    return world


ARGS = ["--expect-model", MODEL, "--user-id", "dev-user"]


def test_without_a_cap_the_run_only_estimates(monkeypatch, tmp_path, capsys):
    world = build_world(monkeypatch, tmp_path)
    assert s5.main(ARGS) == 0
    out = capsys.readouterr().out
    assert "no cap given: no classify call was made" in out and "total: $" in out
    assert out.count("account acc-") == 2 and "owner zone Europe/Berlin" in out
    assert all(agent.calls == 0 for agent in world.agents.values())
    assert sorted(world.built) == ["dev-user", "owner-2"]
    assert list(tmp_path.iterdir()) == []


def test_another_model_stops_the_run_before_the_estimate(monkeypatch, tmp_path, capsys):
    world = build_world(monkeypatch, tmp_path)
    assert s5.main(["--expect-model", "claude-opus-5-5", "--user-id", "dev-user", "--budget-usd", "5"]) == 1
    out = capsys.readouterr().out
    assert "expected claude-opus-5-5" in out and "total:" not in out
    assert all(agent.calls == 0 for agent in world.agents.values()) and list(tmp_path.iterdir()) == []


def test_a_live_token_that_is_not_the_snapshot_stops_the_run(monkeypatch, tmp_path, capsys):
    world = build_world(monkeypatch, tmp_path, drift=True)
    assert s5.main([*ARGS, "--budget-usd", "5"]) == 1
    assert "snapshot_pull --check" in capsys.readouterr().out
    assert all(agent.calls == 0 for agent in world.agents.values())


def test_a_run_with_a_cap_classifies_writes_the_files_and_meters_every_call(monkeypatch, tmp_path, capsys):
    world = build_world(monkeypatch, tmp_path)
    before = copy.deepcopy(world.db.collection(FACTS).docs)
    assert s5.main([*ARGS, "--budget-usd", "5"]) == 0
    out = capsys.readouterr().out
    assert "spent so far (all classify runs): $0.0000" in out and "decided: 3" in out
    assert sorted(p.name.split("_", 4)[-1] for p in tmp_path.glob("fact_s5_classify_2*")) == ["plan.json", "report.md"]
    assert all(agent.calls == 1 for agent in world.agents.values())
    ledger = json.loads((tmp_path / "fact_s5_classify_spend.json").read_text())
    assert ledger["calls"] == 2 and ledger["total_usd"] > 0
    assert world.db.collection(FACTS).docs == before
    for secret in (CAR, MARIA, PIPE, OWNER_ACC, OTHER_ACC):
        assert secret not in out
    CostMeter(tmp_path / "fact_s5_classify_spend.json", 1).close()   # the ledger lock was released


def test_a_second_run_starts_from_what_the_first_spent(monkeypatch, tmp_path, capsys):
    world = build_world(monkeypatch, tmp_path)
    seeded = CostMeter(tmp_path / "fact_s5_classify_spend.json", 100)
    seeded.record(MODEL, LLMResponse(text="", usage_metadata=UsageMetadata(prompt_tokens=3_000_000)))   # $6
    seeded.close()
    assert s5.main([*ARGS, "--budget-usd", "5"]) == 1
    out = capsys.readouterr().out
    assert "is above the cap $5.00 (the cap covers all classify runs)" in out
    assert "spent so far (all classify runs): $6.0000" in out
    assert all(agent.calls == 0 for agent in world.agents.values())


def test_a_cap_between_the_low_and_the_high_estimate_warns_and_goes_on(monkeypatch, tmp_path, capsys):
    world = build_world(monkeypatch, tmp_path)
    assert s5.main(ARGS) == 0
    total = [line for line in capsys.readouterr().out.split("\n") if line.startswith("total: $")][0]
    low, high = (float(x) for x in total.removeprefix("total: $").replace("$", "").split("-"))
    assert s5.main([*ARGS, "--budget-usd", str((low + high) / 2 + 0.005)]) == 0
    out = capsys.readouterr().out
    assert "may pass the cap" in out and "is above the cap" not in out
    assert sum(agent.calls for agent in world.agents.values()) == 2


def test_an_unpriced_model_is_refused_before_any_call(monkeypatch, tmp_path, capsys):
    agents = {"dev-user": Standin(None), "owner-2": Standin(None)}
    for agent in agents.values():
        agent.model_name = "no-such-model"
    build_world(monkeypatch, tmp_path, agents)
    assert s5.main(["--expect-model", "no-such-model", "--user-id", "dev-user", "--budget-usd", "5"]) == 1
    assert "has no price" in capsys.readouterr().out
    assert all(agent.calls == 0 for agent in agents.values())


def test_a_stopped_run_exits_with_one_and_keeps_the_plan(monkeypatch, tmp_path, capsys):
    agents = {"dev-user": Failing(None), "owner-2": Failing(None)}
    build_world(monkeypatch, tmp_path, agents)
    assert s5.main([*ARGS, "--budget-usd", "5"]) == 1
    assert "stopped: call failed twice: RuntimeError" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*_not_decided.txt"))) == 1 and len(list(tmp_path.glob("*_stopped_batch.txt"))) == 1
