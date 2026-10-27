"""scripts/migration/fact_s5_classify_rules.py: the pure half of the S5 classify, on plain values."""
import ast
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.migration import fact_s5_classify_rules as rules
from scripts.migration.fact_s5_classify_rules import (
    FALLBACK_INVALID,
    FALLBACK_MISSING,
    FALLBACK_NO_REPORTED,
    OUTCOME_APPLIED,
    SKIP_DECIDED,
    SKIP_NO_ACCOUNT,
    SKIP_NOT_FOUND,
    SKIP_SYSTEM,
    SOURCE_FALLBACK,
    SOURCE_MODEL,
    SOURCE_PINNED,
    SOURCE_UNSURE,
    Decision,
    PlanFileError,
    RawDecision,
    ScannedFact,
    Skipped,
    already_applied,
    apply_fields,
    batches,
    estimate_usd,
    extract_blocks,
    fact_line,
    fallback_decision,
    is_pinned,
    judge as judge_in_zone,
    pinned_decision,
    plan_document,
    plan_entry,
    planned_fields,
    read_answer,
    read_plan,
    render_report,
    report_row,
    scanned_fact,
    select,
    summary,
    summary_lines,
    system_prompt,
    trail_reason,
    user_message,
)
from src.domain.entities import ExpiryKind, FactEntity, FactState
from src.domain.fact_expiry import EXPIRY_NONE, Expiry
from src.domain.fact_lifecycle import IN_PLACE_FIELDS, LIFECYCLE_EVENTS_CAP, lifecycle_event
from src.domain.fact_review_reasons import gives_window_reason, is_revive_reason
from tests.support.clock_instants import BERLIN, HELSINKI, UTC_MIDNIGHT

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "prompts_snapshot" / "tokens" / "system" / "CONSOLIDATION_TAXONOMY.groovy"
INSTRUCTION = ROOT / "scripts" / "migration" / "fact_s5_classify_instruction.txt"
RULES_PY = ROOT / "scripts" / "migration" / "fact_s5_classify_rules.py"

BERLIN_TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def judge(fact, raw, zone=BERLIN):
    return judge_in_zone(fact, raw, zone)


REPORTED = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)


def scanned(fact_id="f1", **over):
    data = {
        "account_id": "acc-12345678", "text": "car service Nov 4, bring the logbook", "domain": "possession",
        "type": "event", "tags": ["car"], "temporal_class": "dynamic", "metadata": {"k": "v"}, "context": "ctx",
        "reported_date": REPORTED, "created_at": REPORTED,
    }
    data.update(over)
    return scanned_fact(fact_id, data, "2026-10-01T09:30:00Z")


def stored_fact(entry, events, **over):
    fields = dict(
        id=entry.fact_id, account_id=entry.account_id, created_by_user_id="usr-1", lineage_id="L1", text="t", type="event",
        expiry_kind=entry.fields["expiry_kind"], ttl_days=entry.fields["ttl_days"],
        expires_at=entry.fields["expires_at"], lifecycle_events=events,
    )
    fields.update(over)
    return FactEntity(**fields)


# ---- extract_blocks and the prompt ------------------------------------------------------------------------------------

def test_extract_blocks_of_the_real_snapshot():
    out = extract_blocks(SNAPSHOT.read_text(encoding="utf-8"))
    for needle in ("kinds_table", "date_rules", "default_expiry", "        expiry: {", "temporal_classes: {"):
        assert needle in out
    assert "conflict_resolution" not in out
    assert "AXIS 3" not in out
    assert out.startswith("        // AXIS 2: Temporal Class")  # the comment line above the first block comes along


def test_extract_blocks_keeps_the_comment_above_and_nothing_else():
    text = "a {\n}\n// about b\nb: {\n  x: \"}\" // }\n}\nc {\n}\n"
    assert extract_blocks(text, ("b",)) == '// about b\nb: {\n  x: "}" // }\n}'
    assert extract_blocks(text, ("b", "c")) == '// about b\nb: {\n  x: "}" // }\n}\n\nc {\n}'


def test_extract_blocks_reads_an_escaped_quote_inside_a_string():
    text = 'b: {\n  x: "say \\"}\\" now"\n}\nafter\n'
    assert extract_blocks(text, ("b",)) == 'b: {\n  x: "say \\"}\\" now"\n}'


def test_extract_blocks_refuses_a_missing_duplicated_or_unclosed_block():
    with pytest.raises(ValueError, match="'expiry'"):
        extract_blocks("temporal_classes: {\n}\n")
    twice = "temporal_classes: {\n}\nexpiry: {\n}\nexpiry {\n}\n"
    with pytest.raises(ValueError, match="'expiry'"):
        extract_blocks(twice)
    with pytest.raises(ValueError, match="'expiry'.*unbalanced"):
        extract_blocks("temporal_classes: {\n}\nexpiry: {\n  a: {\n}\n")
    with pytest.raises(ValueError, match="unbalanced"):
        extract_blocks("x { } }\n", ("x",))


def test_the_instruction_file():
    raw = INSTRUCTION.read_bytes()
    text = raw.decode("ascii")  # ASCII only
    for needle in ("unsure", "reported_date", "n", "kinds_table", "classify_stored_facts"):
        assert needle in text
    assert "A horizon needs no date in the content: it counts from reported_date." in text
    assert "A fact whose reported_date is null cannot take a horizon." in text
    assert "Never invent a date" in text
    assert "A date that was already past on its reported_date is a past event: {kind: none}." in text
    assert "whose date passed after its reported_date keeps {kind: date}" in text
    assert "ttl_days" not in text
    assert not any(line.strip().startswith("Today:") for line in text.splitlines())


def test_system_prompt_holds_both_parts_verbatim_and_refuses_an_empty_instruction():
    blocks = extract_blocks(SNAPSHOT.read_text(encoding="utf-8"))
    instruction = INSTRUCTION.read_text(encoding="ascii")
    prompt = system_prompt(blocks, instruction)
    assert blocks in prompt and instruction.strip() in prompt
    assert prompt.endswith("\n") and not prompt.endswith("\n\n")
    with pytest.raises(ValueError):
        system_prompt(blocks, "  \n")


# ---- scan and select -------------------------------------------------------------------------------------------------

def test_select_one_fact_per_rule():
    facts = [
        scanned("a", account_id=""), scanned("b", account_id="SYSTEM"), scanned("c", expiry_kind="none"),
        scanned("d"),
    ]
    selected, skipped = select(facts)
    assert [f.fact_id for f in selected] == ["d"]
    assert skipped == [Skipped("a", SKIP_NO_ACCOUNT), Skipped("b", SKIP_SYSTEM), Skipped("c", SKIP_DECIDED)]


def test_select_first_rule_wins_and_keeps_scan_order():
    facts = [scanned("z", account_id="", expiry_kind="date"), scanned("y"), scanned("x")]
    selected, skipped = select(facts)
    assert [f.fact_id for f in selected] == ["y", "x"]
    assert skipped == [Skipped("z", SKIP_NO_ACCOUNT)]


def test_select_in_filtered_mode():
    facts = [scanned("a"), scanned("b", expiry_kind="date"), scanned("c")]
    selected, skipped = select(facts, {"a", "b", "gone", "also-gone"})
    assert [f.fact_id for f in selected] == ["a"]
    assert skipped == [Skipped("b", SKIP_DECIDED), Skipped("also-gone", SKIP_NOT_FOUND), Skipped("gone", SKIP_NOT_FOUND)]
    assert select(facts, set()) == ([], [])


def test_scanned_fact_normalises_what_the_collection_holds():
    fact = scanned(tags=["a", 3, None, "b"], domain="AGENT_DIRECTIVE", type="PRINCIPLE")
    assert fact.tags == ("a", "b")
    assert fact.domain == "agent_directive" and fact.type == "principle"
    assert fact.token == "2026-10-01T09:30:00Z" and fact.fact_id == "f1"
    assert is_pinned(fact)
    assert scanned(tags="car").tags == ()
    assert scanned(metadata="x").metadata == {}
    naive = scanned(reported_date=datetime(2026, 10, 1, 9, 30), created_at=datetime(2026, 10, 1, 12, 0, tzinfo=BERLIN_TZ))
    assert naive.reported_date == REPORTED and naive.reported_date.tzinfo is not None
    assert naive.created_at == datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    bare = scanned_fact("g", {}, "t")
    assert (bare.account_id, bare.text, bare.domain, bare.type, bare.tags, bare.metadata) == ("", "", None, None, (), {})
    assert (bare.context, bare.temporal_class, bare.reported_date, bare.expiry_kind) == (None, None, None, None)


def test_a_reported_date_that_is_a_string_reads_as_none_and_a_horizon_then_falls_back():
    fact = scanned(reported_date="2026-10-01T09:30:00Z")
    assert fact.reported_date is None
    decision, code = judge(fact, RawDecision("horizon", None, 14, False, "x"))
    assert (decision, code) == (None, FALLBACK_NO_REPORTED)
    only_date, _ = judge(fact, RawDecision("date", "2026-11-04", None, False, "x"))
    assert only_date is not None  # a date does not need reported_date


def test_is_pinned():
    assert is_pinned(scanned(domain="agent_directive"))
    assert is_pinned(scanned(type="principle"))
    assert is_pinned(scanned(tags=["Mindset"]))
    assert not is_pinned(scanned())
    assert not is_pinned(scanned(domain="not-a-domain", type="not-a-type"))
    assert not is_pinned(scanned(domain=None, type=None))


def test_batches():
    facts = [scanned(f"f{i}") for i in range(7)]
    assert [len(b) for b in batches(facts, 3)] == [3, 3, 1]
    assert [len(b) for b in batches(facts * 4)] == [25, 3]
    assert batches([]) == []


# ---- the request -----------------------------------------------------------------------------------------------------

def test_fact_line_has_exactly_the_listed_keys_and_local_days():
    fact = scanned(reported_date=UTC_MIDNIGHT.utc, created_at=None, account_id="acc-secret")
    line = fact_line(3, fact, HELSINKI)
    assert list(line) == [
        "n", "text", "domain", "temporal_class", "tags", "metadata", "context", "reported_date", "created_at",
    ]
    assert line["n"] == 3 and line["tags"] == ["car"] and line["metadata"] == {"k": "v"}
    assert line["reported_date"] == "2026-10-27 Tue"  # Helsinki is two hours into the day at UTC midnight
    assert fact_line(3, fact, "America/New_York")["reported_date"] == "2026-10-26 Mon"
    assert line["created_at"] is None


def test_user_message_carries_no_ids_and_names_the_problems():
    lines = [fact_line(1, scanned("secret-id", account_id="secret-account"), BERLIN), fact_line(2, scanned("g"), BERLIN)]
    message = user_message(lines)
    first, *rest = message.split("\n")
    assert first == "Stored facts to classify, one JSON object per line:"
    assert [json.loads(r)["n"] for r in rest] == [1, 2]
    assert "secret-id" not in message and "secret-account" not in message
    retry = user_message(lines[:1], {5: "missing", 2: "duplicate"})
    assert retry.split("\n")[0] == (
        "Your previous answer gave no usable decision for these facts: n=2 (duplicate), n=5 (missing). "
        "Decide each of them again."
    )
    assert retry.split("\n")[1] == first
    assert "Café" in user_message([{"n": 1, "text": "Café"}])


# ---- the answer ------------------------------------------------------------------------------------------------------

def answer(*entries):
    return json.dumps({"decisions": list(entries)})


def test_read_answer_valid():
    raw = answer(
        {"n": 1, "kind": "date", "date": "2026-11-04", "unsure": False, "reason": "  Planned event  "},
        {"n": 2, "kind": "horizon", "days": 30, "unsure": True, "reason": "x" * 500},
    )
    decisions, problems = read_answer(raw, {1, 2})
    assert problems == {}
    assert decisions[1] == RawDecision("date", "2026-11-04", None, False, "Planned event")
    assert decisions[2].unsure is True and decisions[2].days == 30 and len(decisions[2].reason) == 200


def test_read_answer_unparseable():
    for bad in (None, "not json", "[]", "{}", '{"decisions": 3}', '"x"'):
        assert read_answer(bad, {1, 2}) == ({}, {1: "unparseable", 2: "unparseable"})
    assert read_answer("```json\n{}\n```", {1}) == ({}, {1: "unparseable"})  # no markdown stripping


def test_read_answer_duplicate_unknown_and_missing():
    raw = answer(
        {"n": 1, "kind": "none", "unsure": False, "reason": "a"},
        {"n": 1, "kind": "none", "unsure": False, "reason": "b"},
        {"n": 1, "kind": "none", "unsure": False, "reason": "c"},
        {"n": 9, "kind": "none", "unsure": False, "reason": "unknown"},
        {"n": True, "kind": "none"},
        {"n": "2", "kind": "none"},
        "not an object",
        {"n": 3, "kind": "none", "unsure": False, "reason": "ok"},
    )
    decisions, problems = read_answer(raw, {1, 2, 3, 4})
    assert set(decisions) == {3}
    assert problems == {1: "duplicate", 2: "missing", 4: "missing"}


def test_unsure_is_true_only_for_a_json_true():
    raw = answer({"n": 1, "kind": "none", "unsure": "yes", "reason": ""}, {"n": 2, "kind": "none", "reason": None})
    decisions, _ = read_answer(raw, {1, 2})
    assert decisions[1].unsure is False and decisions[2].unsure is False and decisions[2].reason == ""


# ---- judge ------------------------------------------------------------------------------------------------------------

def test_judge_date_horizon_none():
    fact = scanned()
    date_decision, code = judge(fact, RawDecision("date", "2026-11-04", None, False, "planned"))
    assert code is None
    assert date_decision == Decision("f1", SOURCE_MODEL, Expiry(ExpiryKind.DATE, on=date(2026, 11, 4)), None, "date", "planned")
    horizon, _ = judge(fact, RawDecision("horizon", None, 90, False, ""))
    assert horizon.expiry == Expiry(ExpiryKind.HORIZON, days=90) and horizon.model_kind == "horizon"
    none, _ = judge(fact, RawDecision("none", None, None, False, ""))
    assert none.expiry == EXPIRY_NONE and none.source == SOURCE_MODEL


def test_judge_does_not_refuse_a_date_in_the_past():
    decision, code = judge(scanned(reported_date=datetime(2018, 12, 1, tzinfo=timezone.utc)), RawDecision("date", "2019-01-02", None, False, "long gone"))
    assert code is None and decision.expiry.on == date(2019, 1, 2)


def test_judge_unsure_is_none_and_remembers_the_kind():
    decision, code = judge(scanned(), RawDecision("horizon", None, 30, True, "no date"))
    assert code is None
    assert decision.source == SOURCE_UNSURE and decision.expiry == EXPIRY_NONE
    assert decision.model_kind == "horizon" and decision.reason == "no date" and decision.code is None


def test_judge_unusable_answers():
    fact = scanned()
    assert judge(fact, None) == (None, FALLBACK_MISSING)
    assert judge(fact, RawDecision("date", "2026-02-30", None, False, "")) == (None, FALLBACK_INVALID)
    assert judge(fact, RawDecision("horizon", None, 0, False, "")) == (None, FALLBACK_INVALID)
    assert judge(fact, RawDecision("soon", None, None, False, "")) == (None, FALLBACK_INVALID)
    assert judge(scanned(reported_date=None), RawDecision("horizon", None, 5, False, "")) == (None, FALLBACK_NO_REPORTED)


def test_pinned_and_fallback_decisions():
    fact = scanned("p")
    pinned = pinned_decision(fact)
    assert (pinned.fact_id, pinned.source, pinned.expiry, pinned.code, pinned.reason) == ("p", SOURCE_PINNED, EXPIRY_NONE, None, "")
    fallback = fallback_decision(fact, FALLBACK_MISSING)
    assert (fallback.source, fallback.expiry, fallback.code, fallback.reason) == (SOURCE_FALLBACK, EXPIRY_NONE, "missing", "")


# ---- planned fields --------------------------------------------------------------------------------------------------

def test_planned_fields_for_a_date_in_berlin_end_at_the_next_local_midnight():
    decision = Decision("f1", SOURCE_MODEL, Expiry(ExpiryKind.DATE, on=date(2026, 11, 4)), None, "date", "")
    fields = planned_fields(decision, scanned(), BERLIN)
    assert fields == {
        "expiry_kind": ExpiryKind.DATE, "ttl_days": None, "expires_at": datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc),
    }


def test_planned_fields_for_a_horizon_and_none():
    horizon = Decision("f1", SOURCE_MODEL, Expiry(ExpiryKind.HORIZON, days=14), None, "horizon", "")
    fields = planned_fields(horizon, scanned(), BERLIN)
    assert fields["ttl_days"] == 14 and fields["expires_at"] == REPORTED + timedelta(days=14)
    assert planned_fields(pinned_decision(scanned()), scanned(reported_date=None), BERLIN) == {
        "expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None,
    }


def test_a_date_long_before_now_is_planned_and_its_row_is_due_now():
    fact = scanned(reported_date=datetime(2025, 1, 1, tzinfo=timezone.utc))
    decision, _ = judge(fact, RawDecision("date", "2025-03-01", None, False, "an old plan"))
    fields = planned_fields(decision, fact, BERLIN)
    row = report_row(fact, decision, fields, BERLIN, NOW)
    assert fields["expires_at"] == datetime(2025, 3, 1, 23, 0, tzinfo=timezone.utc)
    assert row["due_now"] is True


# ---- trail and apply -------------------------------------------------------------------------------------------------

def test_trail_reasons_are_never_revive_or_window_reasons():
    assert trail_reason(SOURCE_MODEL, None) == "expiry classified at S5 (model)"
    assert trail_reason(SOURCE_PINNED, None) == "expiry classified at S5 (pinned)"
    assert trail_reason(SOURCE_UNSURE, None) == "expiry classified at S5 (unsure)"
    assert trail_reason(SOURCE_FALLBACK, "missing") == "expiry classified at S5 (fallback: missing)"
    for source, code in ((SOURCE_MODEL, None), (SOURCE_PINNED, None), (SOURCE_UNSURE, None), (SOURCE_FALLBACK, "missing")):
        reason = trail_reason(source, code)
        assert not is_revive_reason(reason) and not gives_window_reason(reason)


def make_entry(fact_id="f1", source=SOURCE_MODEL, code=None, **fields):
    base = {"expiry_kind": ExpiryKind.DATE, "ttl_days": None, "expires_at": datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc)}
    base.update(fields)
    fact = scanned(fact_id)
    decision = Decision(fact_id, source, EXPIRY_NONE, code, None, "")
    return plan_entry(decision, fact, base)


def test_apply_fields_has_exactly_the_four_keys_and_one_current_to_current_entry():
    entry = make_entry()
    fields = apply_fields(entry, [], NOW)
    assert set(fields) == {"expiry_kind", "ttl_days", "expires_at", "lifecycle_events"}
    assert set(fields) <= IN_PLACE_FIELDS
    assert fields["expiry_kind"] is ExpiryKind.DATE and fields["expires_at"] == entry.fields["expires_at"]
    [event] = fields["lifecycle_events"]
    assert event == lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "migration", "expiry classified at S5 (model)")
    assert (event["from"], event["to"], event["actor"]) == ("current", "current", "migration")


def test_apply_fields_keeps_the_cap_with_the_new_entry_last():
    old = [lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "x", f"r{i}") for i in range(20)]
    fields = apply_fields(make_entry(), old, NOW)
    events = fields["lifecycle_events"]
    assert len(events) == LIFECYCLE_EVENTS_CAP == 20
    assert events[-1]["actor"] == "migration" and events[0]["reason"] == "r1"
    assert len(old) == 20 and old[0]["reason"] == "r0"  # the input is not mutated


# ---- the plan file ---------------------------------------------------------------------------------------------------

def build_doc(entries, **over):
    kwargs = dict(
        collection="facts", models={"acc-1234": "m"}, generated_at=NOW, taxonomy_sha256="a", instruction_sha256="b",
        stopped=None,
    )
    kwargs.update(over)
    return plan_document(entries, [Skipped("s", SKIP_SYSTEM)], ["nd1"], **kwargs)


def entries_of_each_kind():
    horizon = {"expiry_kind": ExpiryKind.HORIZON, "ttl_days": 30, "expires_at": REPORTED + timedelta(days=30)}
    none = {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}
    return [make_entry("a"), make_entry("b", **horizon), make_entry("c", source=SOURCE_FALLBACK, code="missing", **none)]


def test_plan_document_round_trips_through_json_and_read_plan():
    entries = entries_of_each_kind()
    doc = json.loads(json.dumps(build_doc(entries, stopped="spend cap")))
    assert doc["kind"] == "fact_s5_classify_plan" and doc["version"] == 1 and doc["stopped"] == "spend cap"
    assert doc["generated_at"] == NOW.isoformat() and doc["collection"] == "facts"
    assert doc["prompt"] == {"taxonomy_sha256": "a", "instruction_sha256": "b"} and doc["models"] == {"acc-1234": "m"}
    assert doc["skipped"] == [{"fact_id": "s", "reason": "system_account"}] and doc["not_decided"] == ["nd1"]
    assert doc["decisions"][0]["fields"]["expiry_kind"] == "date"
    assert doc["decisions"][0]["fields"]["expires_at"] == "2026-11-04T23:00:00+00:00"
    assert doc["decisions"][2]["code"] == "missing"
    back = read_plan(doc, "facts")
    assert back == entries
    assert all(e.fields["expires_at"] is None or e.fields["expires_at"].tzinfo is not None for e in back)


def mutated(entry_index=0, **fields):
    doc = json.loads(json.dumps(build_doc(entries_of_each_kind())))
    doc["decisions"][entry_index]["fields"].update(fields)
    return doc


def test_read_plan_refuses_a_foreign_or_damaged_document():
    good = json.loads(json.dumps(build_doc(entries_of_each_kind())))
    with pytest.raises(PlanFileError):
        read_plan(good, "other_collection")
    with pytest.raises(PlanFileError):
        read_plan({**good, "kind": "something"}, "facts")
    with pytest.raises(PlanFileError):
        read_plan({**good, "version": 2}, "facts")
    with pytest.raises(PlanFileError):
        read_plan({**good, "decisions": "x"}, "facts")
    with pytest.raises(PlanFileError):
        read_plan([], "facts")
    dup = json.loads(json.dumps(build_doc([make_entry("a"), make_entry("a")])))
    with pytest.raises(PlanFileError, match="entry 1.*duplicate"):
        read_plan(dup, "facts")


@pytest.mark.parametrize("fields", [
    {"expires_at": "2026-11-04T23:00:00"},           # naive
    {"expires_at": "garbage"},
    {"expires_at": 5},
    {"expiry_kind": "never"},
    {"ttl_days": True},
    {"ttl_days": 1.5},
    {"ttl_days": 5},                                  # a date entry with ttl_days
    {"expires_at": "9999-06-01T00:00:00+00:00"},      # year 9999, refused by expiry_refusal
    {"expires_at": None},                             # a date without its data
])
def test_read_plan_refuses_a_date_entry_with(fields):
    with pytest.raises(PlanFileError, match="plan entry 0"):
        read_plan(mutated(0, **fields), "facts")


@pytest.mark.parametrize("fields", [
    {"ttl_days": None},        # a horizon without ttl_days
    {"ttl_days": 0},
    {"ttl_days": 36501},
    {"expires_at": None},
])
def test_read_plan_refuses_a_horizon_entry_with(fields):
    with pytest.raises(PlanFileError, match="plan entry 1"):
        read_plan(mutated(1, **fields), "facts")


def test_read_plan_refuses_a_none_entry_with_data_and_bad_envelopes():
    with pytest.raises(PlanFileError, match="plan entry 2"):
        read_plan(mutated(2, expires_at="2026-11-04T23:00:00+00:00"), "facts")
    with pytest.raises(PlanFileError, match="plan entry 2"):
        read_plan(mutated(2, ttl_days=3), "facts")
    for key, value in (("fact_id", ""), ("account_id", None), ("token", 5), ("source", "guess"), ("code", "odd"), ("fields", None)):
        doc = mutated()
        doc["decisions"][0][key] = value
        with pytest.raises(PlanFileError, match="plan entry 0"):
            read_plan(doc, "facts")
    doc = mutated()
    doc["decisions"][0] = "x"
    with pytest.raises(PlanFileError, match="plan entry 0"):
        read_plan(doc, "facts")


def test_nothing_is_returned_when_a_late_entry_is_bad():
    doc = mutated(2, ttl_days=3)
    with pytest.raises(PlanFileError):
        read_plan(doc, "facts")


# ---- already_applied -------------------------------------------------------------------------------------------------

def test_already_applied():
    entry = make_entry()
    mine = lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "migration", trail_reason(entry.source, entry.code))
    assert already_applied(entry, stored_fact(entry, [mine]))
    earlier = lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "x", "older")
    assert already_applied(entry, stored_fact(entry, [earlier, mine]))
    # the fields differ
    assert not already_applied(entry, stored_fact(entry, [mine], expires_at=entry.fields["expires_at"] + timedelta(days=1)))
    assert not already_applied(entry, stored_fact(entry, [mine], expiry_kind=ExpiryKind.NONE, expires_at=None))
    assert not already_applied(entry, stored_fact(entry, [mine], ttl_days=3))
    # another actor wrote last
    janitor = lifecycle_event(NOW, FactState.CURRENT, FactState.STALE, "janitor", "expiry passed")
    assert not already_applied(entry, stored_fact(entry, [mine, janitor], state=FactState.STALE))
    # another source's reason
    other = lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "migration", trail_reason(SOURCE_UNSURE, None))
    assert not already_applied(entry, stored_fact(entry, [other]))
    assert not already_applied(entry, stored_fact(entry, []))


def test_already_applied_compares_expires_at_in_utc():
    entry = make_entry()
    mine = lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "migration", trail_reason(entry.source, entry.code))
    berlin = entry.fields["expires_at"].astimezone(timezone(timedelta(hours=1)))
    assert already_applied(entry, stored_fact(entry, [mine], expires_at=berlin))
    no_expiry = make_entry("n", expiry_kind=ExpiryKind.NONE, expires_at=None)
    mine_none = lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "migration", trail_reason(no_expiry.source, no_expiry.code))
    assert already_applied(no_expiry, stored_fact(no_expiry, [mine_none]))


# ---- report and summary -----------------------------------------------------------------------------------------------

def decided_rows():
    due = scanned("due", text="a trip | long\nago", reported_date=datetime(2024, 12, 1, tzinfo=timezone.utc))
    soon = scanned("soon", text="b horizon fact")
    quiet = scanned("quiet", text="a stable fact")
    d_due, _ = judge(due, RawDecision("date", "2025-01-01", None, False, "old plan"))
    d_soon, _ = judge(soon, RawDecision("horizon", None, 90, False, "activity"))
    d_quiet = pinned_decision(quiet)
    triples = [(quiet, d_quiet), (soon, d_soon), (due, d_due)]
    fields = {f.fact_id: planned_fields(d, f, BERLIN) for f, d in triples}
    rows = [report_row(f, d, fields[f.fact_id], BERLIN, NOW) for f, d in triples]
    return triples, fields, rows


def test_report_row_columns():
    _, _, rows = decided_rows()
    quiet, soon, due = rows
    assert list(due) == [
        "account", "text", "domain", "type", "temporal_class", "reported", "decision", "expires_at", "due_now", "reason", "flag",
    ]
    assert due["account"] == "acc-1234" and due["decision"] == "date 2025-01-01" and due["due_now"] is True
    assert due["expires_at"] == "2025-01-02 Thu 00:00 CET" and due["reported"] == "2024-12-01 Sun"
    assert due["flag"] == "" and due["reason"] == "old plan"
    assert soon["decision"] == "horizon 90 d" and soon["due_now"] is False and soon["expires_at"].startswith("2026-12-30")
    assert quiet["decision"] == "none" and quiet["expires_at"] == "" and quiet["flag"] == "pinned" and quiet["due_now"] is False


def test_report_row_flags_and_missing_reported_date():
    fact = scanned(reported_date=None)
    unsure, _ = judge(fact, RawDecision("date", "2026-11-04", None, True, "no idea"))
    row = report_row(fact, unsure, planned_fields(unsure, fact, BERLIN), BERLIN, NOW)
    assert row["flag"] == "unsure (model said date)" and row["reported"] == ""
    fallback = fallback_decision(fact, FALLBACK_INVALID)
    row = report_row(fact, fallback, planned_fields(fallback, fact, BERLIN), BERLIN, NOW)
    assert row["flag"] == "fallback: invalid_expiry"


def test_render_report_orders_due_rows_first_and_escapes_cells():
    _, _, rows = decided_rows()
    out = render_report(rows, ["decided: 3", "due_now: 1"])
    lines = out.split("\n")
    assert lines[0] == "# Facts S5 classify — dry run"
    assert "- decided: 3" in lines and "- due_now: 1" in lines
    header = next(i for i, line in enumerate(lines) if line.startswith("| account"))
    body = lines[header + 2:header + 5]
    assert "a trip \\| long ago" in body[0] and "| yes |" in body[0]
    assert "b horizon fact" in body[1] and "| no |" in body[1]
    assert "a stable fact" in body[2]
    assert out.endswith("\n") and "\n\n\n" not in out


def test_render_report_sorts_by_kind_then_text_inside_the_not_due_group():
    base = {c: "" for c in ("account", "domain", "type", "temporal_class", "reported", "expires_at", "reason", "flag")}
    rows = [
        {**base, "text": "z", "decision": "none", "due_now": False},
        {**base, "text": "y", "decision": "horizon 3 d", "due_now": False},
        {**base, "text": "x", "decision": "date 2026-12-01", "due_now": False},
        {**base, "text": "a", "decision": "none", "due_now": False},
    ]
    out = render_report(rows, [])
    order = [line.split("|")[2].strip() for line in out.split("\n") if line.startswith("| ") and "account" not in line and "---" not in line]
    assert order == ["x", "y", "a", "z"]


def test_summary_counts_on_a_mixed_set():
    triples, fields, _ = decided_rows()
    decisions = [d for _, d in triples] + [fallback_decision(scanned("fb"), FALLBACK_NO_REPORTED), judge(scanned("u"), RawDecision("date", None, None, True, ""))[0]]
    fields = {**fields, "fb": planned_fields(decisions[3], scanned("fb"), BERLIN), "u": planned_fields(decisions[4], scanned("u"), BERLIN)}
    result = summary(
        decisions, fields, [Skipped("s", SKIP_SYSTEM), Skipped("t", SKIP_SYSTEM), Skipped("v", SKIP_DECIDED)], ["n1"], NOW,
        spend={"run_usd": 0.5, "spent_usd": 1.25}, zones={"acc-1234": "Europe/Berlin", "acc-9999": "UTC"}, no_reported_date=1,
    )
    assert result["decided"] == 5
    assert result["by_kind"] == {"date": 1, "horizon": 1, "none": 3}
    assert result["by_source"] == {"model": 2, "pinned": 1, "unsure": 1, "past_at_report": 0, "fallback": 1}
    assert result["fallback_by_code"] == {"no_reported_date": 1}
    assert result["due_now"] == 1
    assert result["skipped"] == {"system_account": 2, "already_decided": 1}
    assert result["not_decided"] == 1 and result["no_reported_date"] == 1
    assert result["cost"] == {"run_usd": 0.5, "spent_usd": 1.25}
    empty = summary([], {}, [], [], NOW)
    assert empty["decided"] == 0 and empty["cost"] == {} and empty["zones"] == {}


def test_summary_lines_show_zones_and_no_ids_or_text():
    triples, fields, _ = decided_rows()
    decisions = [d for _, d in triples]
    result = summary(
        decisions, fields, [Skipped("some-fact-id", SKIP_SYSTEM)], ["another-fact-id"], NOW,
        spend={"run_usd": 0.5}, zones={"acc-1234": "Europe/Berlin", "acc-9999": "UTC"},
    )
    lines = summary_lines(result)
    assert "zone acc-9999: UTC" in lines and "zone acc-1234: Europe/Berlin" in lines
    assert "decided: 3" in lines and "due_now: 1" in lines and "cost run_usd: $0.5000" in lines
    assert "by_kind: date=1, horizon=1, none=1" in lines and "skipped: system_account=1" in lines
    joined = "\n".join(lines)
    assert "some-fact-id" not in joined and "another-fact-id" not in joined and "a stable fact" not in joined
    assert "skipped: none" in summary_lines(summary([], {}, [], [], NOW))


def test_estimate_usd_pins_the_plans_arithmetic():
    calls = [(20_460, 25)] * 21
    low, high = estimate_usd("claude-opus-5-5", calls)
    assert 1.8 <= low <= high <= 3.7
    half_low, half_high = estimate_usd("claude-sonnet-5-5", calls)
    assert half_low == pytest.approx(low / 2, rel=0.01) and half_high == pytest.approx(high / 2, rel=0.01)
    assert estimate_usd("no-such-model", calls) is None
    assert estimate_usd("claude-opus-5-5", []) == (0.0, 0.0)


def test_outcome_names_are_the_contract_values():
    assert rules.OUTCOME_APPLIED == OUTCOME_APPLIED == "applied"
    assert (rules.OUTCOME_ALREADY, rules.OUTCOME_CHANGED, rules.OUTCOME_FAILED) == ("already_applied", "skipped_changed", "failed")
    assert rules.BATCH_SIZE == 25 and rules.TRAIL_ACTOR == "migration" and rules.SYSTEM_ACCOUNT == "SYSTEM"


def test_the_response_schema_asks_for_every_decision_field():
    item = rules.RESPONSE_SCHEMA["properties"]["decisions"]["items"]
    assert item["required"] == ["n", "kind", "unsure", "reason"]
    assert item["properties"]["kind"]["enum"] == ["date", "horizon", "none"]


def test_the_module_imports_only_stdlib_and_src_domain():
    tree = ast.parse(RULES_PY.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module or "")
    outside = {r for r in roots if r.split(".")[0] not in rules_stdlib() and not r.startswith("src.domain")}
    assert outside == set()


def rules_stdlib():
    import sys
    return set(sys.stdlib_module_names)


# ---- a date already past when stated ----------------------------------------------------------------------------------

def test_a_date_before_the_reported_local_day_is_a_past_event():
    fact = scanned(reported_date=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
    decision, code = judge(fact, RawDecision("date", "2026-10-09", None, False, "it took place"))
    assert code is None
    assert decision.source == rules.SOURCE_PAST == "past_at_report" and decision.expiry == EXPIRY_NONE
    assert decision.model_kind == "date 2026-10-09" and decision.reason == "it took place"
    fields = planned_fields(decision, fact, BERLIN)
    assert fields == {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}
    row = report_row(fact, decision, fields, BERLIN, NOW)
    assert row["flag"] == "past at report (model said date 2026-10-09)" and row["decision"] == "none"
    assert trail_reason(decision.source, decision.code) == "expiry classified at S5 (past_at_report)"
    assert not is_revive_reason("expiry classified at S5 (past_at_report)")


def test_a_date_on_or_after_the_reported_local_day_is_kept():
    fact = scanned(reported_date=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
    for day in ("2026-10-10", "2026-10-11"):
        decision, _ = judge(fact, RawDecision("date", day, None, False, ""))
        assert decision.source == SOURCE_MODEL and decision.expiry.on.isoformat() == day


def test_the_reported_day_is_read_in_the_owner_zone():
    at_utc_midnight = scanned(reported_date=UTC_MIDNIGHT.utc)             # 2026-10-27 00:00 UTC
    # In New York it is still the 26th, so the 26th is the reported day and is kept; in Helsinki it is the 27th.
    assert judge(at_utc_midnight, RawDecision("date", "2026-10-26", None, False, ""), "America/New_York")[0].source == SOURCE_MODEL
    assert judge(at_utc_midnight, RawDecision("date", "2026-10-26", None, False, ""), HELSINKI)[0].source == rules.SOURCE_PAST


def test_a_past_at_report_entry_survives_the_plan_and_the_apply_checks():
    fact = scanned()
    decision = Decision("f1", rules.SOURCE_PAST, EXPIRY_NONE, None, "date 2020-01-01", "")
    entry = plan_entry(decision, fact, planned_fields(decision, fact, BERLIN))
    [back] = read_plan(json.loads(json.dumps(build_doc([entry]))), "facts")
    assert back == entry and back.source == "past_at_report"
    mine = lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "migration", trail_reason(entry.source, entry.code))
    assert already_applied(entry, stored_fact(entry, [mine]))
    assert summary([decision], {"f1": planned_fields(decision, fact, BERLIN)}, [], [], NOW)["by_source"]["past_at_report"] == 1


def test_read_plan_reads_a_closed_state_and_refuses_an_unknown_one():
    doc = mutated()
    doc["decisions"][0]["state"] = "archived"
    assert read_plan(doc, "facts")[0].state == "archived"
    del doc["decisions"][0]["state"]
    assert read_plan(doc, "facts")[0].state == "current"     # a plan written before the field existed
    doc["decisions"][0]["state"] = "superseded"
    with pytest.raises(PlanFileError, match="plan entry 0.*state"):
        read_plan(doc, "facts")
