"""Bench 7 `--stage-ids` / `--expiry-plan`: exactly the listed facts, in any state, staged stale in the kit's overlay with
the plan's expiry, and the report that says what the review chose for each. The plan reader and the ids reader."""
import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.consolidation import plan_inputs
from scripts.consolidation import review_bench_support as support
from scripts.consolidation import test_review_dryrun as review_bench
from scripts.consolidation.s3_dry_run_kit import DryRunKit
from src.domain.entities import ExpiryKind, FactEntity, FactState, FactType
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import REVIEW_BATCH
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_kit import ACC, USER
from tests.support.fact_review_world import ReviewWorld
from tests.unit.scripts.test_review_benches import CLOCK, review_rig, tripwires
from tests.unit.scripts.test_review_kit import live_state
from tests.unit.scripts.test_s3_dry_run_kit import answered, memory_dir, store  # noqa: F401  (fixtures)

PLAN_EXPIRES = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)


def plan_doc(*decisions):
    return {"kind": plan_inputs.PLAN_KIND, "version": plan_inputs.PLAN_VERSION, "collection": "c", "decisions": list(decisions)}


def decision(fact_id, kind="date", ttl_days=None, expires_at=PLAN_EXPIRES.isoformat()):
    return {"fact_id": fact_id, "account_id": "a", "token": "t", "source": "model",
            "fields": {"expiry_kind": kind, "ttl_days": ttl_days, "expires_at": expires_at}}


# ------------------------------------------------------------------------------------------------ the readers

def test_read_ids_skips_comments_blanks_and_duplicates(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text("a\n\n# note\nb  # tail\na\n", encoding="utf-8")

    assert plan_inputs.read_ids(path, tmp_path) == ["a", "b"]


def test_a_file_outside_the_memory_folder_is_refused(tmp_path):
    outside = tmp_path / "ids.txt"
    outside.write_text("a\n", encoding="utf-8")

    with pytest.raises(plan_inputs.SelectionError):
        plan_inputs.read_ids(outside, tmp_path / "memory")


def test_the_plan_reader_returns_the_expiry_fields_by_fact_id(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan_doc(
        decision("f1"), decision("f2", "horizon", 30, PLAN_EXPIRES.isoformat()), decision("f3", "none", None, None),
    )), encoding="utf-8")

    plan = plan_inputs.read_expiry_plan(path, tmp_path)

    assert plan["f1"] == {"expiry_kind": ExpiryKind.DATE, "ttl_days": None, "expires_at": PLAN_EXPIRES}
    assert plan["f2"]["ttl_days"] == 30 and plan["f3"] == {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}


@pytest.mark.parametrize("doc, why", [
    ("{", "not JSON"),
    ({"kind": "other", "version": 1, "decisions": []}, "not a classify plan"),
    ({"kind": plan_inputs.PLAN_KIND, "version": 1, "decisions": "x"}, "no decisions list"),
    (plan_doc({"fields": {}}), "fact_id or fields missing"),
    (plan_doc(decision("f1"), decision("f1")), "duplicate fact_id"),
    (plan_doc(decision("f1", kind="forever")), "unknown expiry_kind"),
    (plan_doc(decision("f1", ttl_days="3")), "ttl_days is not a whole number"),
    (plan_doc(decision("f1", expires_at="soon")), "expires_at is not a datetime"),
    (plan_doc(decision("f1", expires_at="2026-11-20T22:00:00")), "not timezone-aware"),
])
def test_a_malformed_plan_is_refused(tmp_path, doc, why):
    path = tmp_path / "plan.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")

    with pytest.raises(plan_inputs.SelectionError, match=why if doc != "{" else "not JSON"):
        plan_inputs.read_expiry_plan(path, tmp_path)


# ------------------------------------------------------------------------------------------------ the options

def _parse(tmp_path, *argv):
    ap = review_bench.build_parser()
    args = ap.parse_args(["--s3-kit", *argv])
    return ap, args


def test_the_options_are_read_into_ids_and_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_inputs, "MEMORY_DIR", tmp_path)
    (tmp_path / "ids.txt").write_text("f1\nf2\n", encoding="utf-8")
    (tmp_path / "plan.json").write_text(json.dumps(plan_doc(decision("f1"))), encoding="utf-8")
    ap, args = _parse(tmp_path, "--stage-ids", str(tmp_path / "ids.txt"), "--expiry-plan", str(tmp_path / "plan.json"))

    review_bench.read_staging_inputs(ap, args)

    assert args.listed_ids == ["f1", "f2"] and set(args.plan) == {"f1"}


def test_without_the_options_nothing_is_listed(tmp_path):
    ap, args = _parse(tmp_path)

    review_bench.read_staging_inputs(ap, args)

    assert args.listed_ids is None and args.plan is None


def test_a_plan_without_ids_an_empty_list_a_big_list_and_a_foreign_file_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_inputs, "MEMORY_DIR", tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(plan_doc()), encoding="utf-8")
    (tmp_path / "empty.txt").write_text("# nothing\n", encoding="utf-8")
    (tmp_path / "big.txt").write_text("\n".join(f"f{i}" for i in range(REVIEW_BATCH + 1)), encoding="utf-8")
    outside = tmp_path.parent / "ids-outside.txt"
    outside.write_text("f1\n", encoding="utf-8")
    cases = [
        ("--expiry-plan", str(tmp_path / "plan.json")),
        ("--stage-ids", str(tmp_path / "empty.txt")),
        ("--stage-ids", str(tmp_path / "big.txt")),
        ("--stage-ids", str(outside)),
        ("--stage-ids", str(tmp_path / "missing.txt")),
    ]
    for option in cases:
        ap, args = _parse(tmp_path, *option)
        with pytest.raises(SystemExit):
            review_bench.read_staging_inputs(ap, args)


def test_the_banner_names_the_listed_facts():
    args = argparse.Namespace(runs=1, stage=18, listed_ids=["a", "b"])

    assert "--stage-ids 2 fact(s)" in review_bench.header(args)[1]


# ------------------------------------------------------------------------------------------------ the staging

async def _rig(tmp_path, monkeypatch, *seeds):
    """A world with live facts in the given states, the kit over it and the stand-ins."""
    world = ReviewWorld()
    tripwires(monkeypatch)
    ids = []
    for text, state in seeds:
        extra = {"valid_to": NOW - timedelta(days=1)} if state is not FactState.CURRENT else {}
        ids.append(await world.seed(text, state=state, **extra))
    handles, args, live = await review_rig(world, tmp_path, monkeypatch, answered())
    kit = DryRunKit(ledger_path=tmp_path / "spend.json", with_review=True)
    kit.install(handles.agent)
    bundle = kit.review_stand_ins(account_repo=handles.account_repo, user_repo=handles.user_repo, clock=CLOCK)
    return world, kit, bundle, ids


async def test_listed_facts_in_any_state_are_staged_stale_with_the_plans_expiry(tmp_path, monkeypatch):
    world, kit, bundle, (talk, fine, gym) = await _rig(
        tmp_path, monkeypatch,
        ("Alex gives a talk at the Hamburg conference", FactState.ARCHIVED),
        ("Alex has an open parking fine", FactState.INVALIDATED),
        ("Alex trains at the Iron Temple gym", FactState.CURRENT),
    )
    before = live_state(world)
    plan = {talk: {"expiry_kind": ExpiryKind.DATE, "ttl_days": None, "expires_at": PLAN_EXPIRES}}

    staged = await support.stage_inputs(
        kit, bundle, user_id=USER, account_id=ACC, requested=5, now=NOW, channel="D1", listed_ids=[talk, fine, gym],
        expiry_plan=plan,
    )

    assert staged.listed == {talk: "archived", fine: "invalidated", gym: "current"}
    assert staged.cases == {} and staged.live == [] and staged.questions == {}
    for fact_id in (talk, fine, gym):
        doc = kit.repo.overlay_document(fact_id)
        assert (doc.state, doc.is_current, doc.valid_to) == (FactState.STALE, True, None) and doc.stale_since == NOW
    staged_talk = kit.repo.overlay_document(talk)
    assert (staged_talk.expiry_kind, staged_talk.expires_at) == (ExpiryKind.DATE, PLAN_EXPIRES)
    assert kit.repo.overlay_document(gym).expires_at != PLAN_EXPIRES        # no plan entry: the stored expiry stays
    assert live_state(world) == before                                       # nothing live written
    assert staged.batch == 3
    kit.close()


async def test_a_listed_fact_without_a_document_is_refused_before_anything_is_staged(tmp_path, monkeypatch):
    world, kit, bundle, (gym,) = await _rig(tmp_path, monkeypatch, ("Alex trains at the Iron Temple gym", FactState.CURRENT))

    with pytest.raises(ValueError, match="has no document"):
        await support.stage_listed(kit, [gym, "missing"], None, NOW)

    assert kit.repo.overlay_document(gym) is None
    kit.close()


async def test_more_listed_facts_than_one_batch_is_refused(tmp_path, monkeypatch):
    world, kit, bundle, _ = await _rig(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="one review batch"):
        await support.stage_listed(kit, [f"f{i}" for i in range(REVIEW_BATCH + 1)], None, NOW)
    kit.close()


# ------------------------------------------------------------------------------------------------ the report

def _doc(state, reason=None, **over):
    events = [{"at": NOW, "from": "stale", "to": state.value, "actor": "review", "reason": reason}] if reason else []
    return FactEntity(
        account_id=ACC, created_by_user_id=USER, lineage_id="L", text="Alex rents a pottery studio", type=FactType.STATE,
        state=state, lifecycle_events=events, **over,
    )


@pytest.mark.parametrize("doc, asked, outcome", [
    (_doc(FactState.ARCHIVED, "archived on abcd1234"), None, "archive"),
    (_doc(FactState.INVALIDATED, "invalidated on abcd1234"), None, "invalidate"),
    (_doc(FactState.SUPERSEDED, "superseded by x"), None, "supersede"),
    (_doc(FactState.CURRENT, "revived on abcd1234"), None, "revive"),
    (_doc(FactState.CURRENT, "rewritten on abcd1234"), None, "rewrite"),
    (_doc(FactState.CURRENT, "rewritten as history on abcd1234"), None, "history"),
    (_doc(FactState.CURRENT, "looked fine"), None, "current"),
    (_doc(FactState.CURRENT, "queued for the owner q=q1"), "happened", "ask:happened"),
    (_doc(FactState.STALE), None, "left stale"),
    (None, None, "left stale"),
])
def test_the_outcome_the_review_chose(doc, asked, outcome):
    assert support._chosen_outcome(doc, asked) == outcome


async def test_the_listed_report_gives_state_text_outcome_question_kind_and_reason(tmp_path, monkeypatch):
    world, kit, bundle, (talk, gym) = await _rig(
        tmp_path, monkeypatch,
        ("Alex gives a talk at the Hamburg conference", FactState.ARCHIVED),
        ("Alex trains at the Iron Temple gym", FactState.CURRENT),
    )
    listed = await support.stage_listed(kit, [talk, gym], None, NOW)
    from tests.support.review_rule_builders import question as build_question

    bundle.store.seed_question(build_question(QuestionStatus.ASKED, question_id="q-talk", fact_id=talk, question="happened", fallback="history",
        texts={"happened": "Alex gave the talk", "not_happened": "Alex did not give the talk", "unknown": "unknown"},
    ))

    rows = support.listed_report(kit, bundle, listed)

    by_id = {row["fact_id"]: row for row in rows}
    assert by_id[talk]["original_state"] == "archived" and by_id[talk]["outcome"] == "ask:happened"
    assert by_id[talk]["text"] == "Alex gives a talk at the Hamburg conference"
    assert by_id[gym]["outcome"] == "left stale" and by_id[gym]["final_state"] == "stale"
    assert by_id[gym]["reason"] is not None or by_id[gym]["actor"] is None


def test_the_run_counts_name_the_listed_outcomes_and_stay_the_same_without_them():
    kit = DryRunKit.__new__(DryRunKit)
    kit.rows = []
    base = {"rows": [0, 0], "result": {"status": "completed"}, "batch": 1, "legacy": 0, "live_staged": 0, "cases": {},
            "stand_ins": {"questions": [], "posts": [], "texts": [], "tasks": []}, "alerts": []}

    plain = support.run_counts(base, kit)
    listed = support.run_counts({**base, "listed": [{"outcome": "archive"}, {"outcome": "archive"}, {"outcome": "revive"}]}, kit)

    assert listed[0] == "listed facts: archive 2, revive 1" and listed[1:] == plain


async def test_a_whole_run_on_listed_ids_reports_each_fact_and_writes_nothing_live(tmp_path, monkeypatch, capsys):
    from scripts.consolidation import s3_bench_support

    world = ReviewWorld()
    tripwires(monkeypatch)
    talk = await world.seed("Alex gives a talk at the Hamburg conference", state=FactState.ARCHIVED,
                            valid_to=NOW - timedelta(days=1))
    handles, args, live = await review_rig(world, tmp_path, monkeypatch, answered())
    before = live_state(world)
    async with s3_bench_support.kit_session(
        handles, args, USER, ACC, "review", with_review=True, summary=support.review_summary,
    ) as (kit, extra):
        run = await support.run_once(
            kit, handles, live, user_id=USER, account_id=ACC, requested=5, mirror=None, clock=CLOCK, listed_ids=[talk],
            expiry_plan={talk: {"expiry_kind": ExpiryKind.DATE, "ttl_days": None, "expires_at": PLAN_EXPIRES}},
        )
        extra["runs"] = [run]

    assert [(r["fact_id"], r["original_state"]) for r in run["listed"]] == [(talk, "archived")]
    assert run["cases"] == {} and run["live_staged"] == 0
    assert live_state(world) == before
    assert "listed facts:" in "\n".join(support.run_counts(run, kit))
