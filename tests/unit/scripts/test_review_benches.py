"""Facts S4 · T12b: the two memory-review benches run the real pieces on the kit and touch nothing live.

Covered: `build_review_service` (every port the kit or its stand-ins own, the coordinator stand-in that calls `execute` on the
kit agent and never the real coordinator or `process`); the review bench's staging and a whole run with a scripted model
(nothing live changes, the task and label its gate reads); the answer bench's windows (the production history shapes), its
judge and a case through the real Stage 1; the small pure helpers and the gate lines.

The live side is the review world: the real repository, review store and session store on one evaluating Firestore fake.
"Nothing live" is that fake's documents and change stamps staying byte-identical.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from scripts.consolidation import review_bench_cases as cases
from scripts.consolidation import review_bench_support as support
from scripts.consolidation import s3_bench_support
from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation import test_review_answers_dryrun as answers_bench
from scripts.consolidation import test_review_dryrun as review_bench
from scripts.consolidation.review_kit_stand_ins import KitCoordinator, build_review_service
from scripts.consolidation.s3_dry_run_kit import DryRunKit
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.entities import FactDomain, FactState
from src.domain.fact_review_frame import not_delivered_frame, question_text, reply_part, review_frame
from src.domain.fact_review_language import CANONICAL_LABELS, LABEL_KEYS, fact_question_message
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.locales.uk import UI_STRINGS as UK_STRINGS
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reasons import queued_reason
from src.domain.fact_review_rules import REVIEW_BATCH, queued_question
from src.domain.fact_review_state import FactReviewState
from src.domain.llm import ToolCall
from src.domain.user import UserBotConfig, UserProfile
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import Intent
from tests.support.fact_kit import ACC, USER
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.unit.agents.test_consolidation_write_context import calls, create, make_agent
from tests.unit.scripts.test_review_kit import Accounts, LISBON, RefusingBlueprints, Users, live_state
from tests.unit.scripts.test_s3_dry_run_kit import (  # noqa: F401  (fixtures: store, memory_dir)
    SONNET,
    answered,
    assembly,
    kit_agent,
    memory_dir,
    metered,
    scripted,
    store,
)

CLOCK = lambda: NOW  # noqa: E731  (the one clock of a test run)


# ---------------------------------------------------------------------------------------------- the world and the handles

class Opened:
    """The live account as the bench reads it: the owner's primary channel is a Slack DM."""

    def __init__(self, world: ReviewWorld) -> None:
        self.world = world

    async def get_primary(self, user_id: str):
        return await self.world.notification_state.get_primary(user_id)


async def review_rig(world: ReviewWorld, tmp_path, monkeypatch, *responses):
    """A kit `with_review` installed on a consolidation agent over the world's live repository, the handles the bench takes,
    and the live notification state. The model answers `responses` in order, then reports nothing."""
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", tmp_path / "spend.json")
    agent = make_agent(world.store, scripted(*responses))
    agent.model_name = SONNET
    service = assembly()
    service.blueprint_repo = RefusingBlueprints()
    agent.prompt_builder.assembly_service = service
    await world.notification_state.save_primary(USER, "slack", "D1")
    handles = SimpleNamespace(
        account_repo=Accounts(), user_repo=Users(), agent=agent,
        container=SimpleNamespace(session_store=world.session_store, fact_review_store=world.review_store),
    )
    args = SimpleNamespace(budget_usd=30.0, allow_over_owner_cap=False, timezone=LISBON)
    return handles, args, Opened(world)


def tripwires(monkeypatch) -> None:
    """The two doors the kit must never use: the real coordinator, and `process` (its `_flush_billing` writes the account's
    live usage counters)."""
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("the real coordinator or process() was reached")

    monkeypatch.setattr(AgentCoordinator, "route_message", boom)
    monkeypatch.setattr(BaseAgent, "process", boom)


# ---------------------------------------------------------------------------------------------- build_review_service

async def test_the_review_service_is_built_from_the_kit_and_its_stand_ins_only(tmp_path, monkeypatch):
    world = ReviewWorld()
    handles, args, _ = await review_rig(world, tmp_path, monkeypatch, answered())
    kit = DryRunKit(ledger_path=tmp_path / "spend.json", with_review=True)
    kit.install(handles.agent)
    bundle = kit.review_stand_ins(account_repo=handles.account_repo, user_repo=handles.user_repo, clock=CLOCK)

    service = build_review_service(kit, bundle, agent=handles.agent, account_repo=handles.account_repo, user_repo=handles.user_repo)

    assert service._repo is kit.repo and service._counters is kit.counters and service._alert_sink is kit.sink
    assert service._review_store is bundle.store and service._lease is bundle.lease
    assert service._task_queue is bundle.task_queue and service._session_store is bundle.sessions
    assert service._interactive is bundle.interactive and service._notifications is bundle.notifier
    assert service._notification_state is bundle.notification_state and service._channel_bindings is bundle.channel_bindings
    assert service._texts is bundle.texts and service._owner_zones is bundle.owner_zones
    assert service._answer_service is bundle.answer_service and service._agent_factory is bundle.agent_factory
    assert service._clock is bundle.clock is CLOCK
    assert isinstance(service._coordinator, KitCoordinator)
    # the two live ports are the read-only ones
    assert service._account_repo is handles.account_repo and service._user_repo is handles.user_repo
    kit.close()


async def test_a_bundle_that_is_not_the_kits_or_an_agent_without_the_kit_is_refused(tmp_path, monkeypatch):
    world = ReviewWorld()
    handles, _, _ = await review_rig(world, tmp_path, monkeypatch, answered())
    other = make_agent(world.store, scripted(answered()))
    kit = DryRunKit(ledger_path=tmp_path / "spend.json", with_review=True)
    kit.install(handles.agent)
    first = kit.review_stand_ins(account_repo=Accounts(), user_repo=Users())
    kit.review_stand_ins(account_repo=Accounts(), user_repo=Users())            # a second bundle: the first is stale
    ports = dict(account_repo=Accounts(), user_repo=Users())

    with pytest.raises(ValueError, match="not the kit's"):
        build_review_service(kit, first, agent=handles.agent, **ports)
    with pytest.raises(ValueError, match="not the kit's"):
        build_review_service(DryRunKit(ledger_path=tmp_path / "other.json"), first, agent=handles.agent, **ports)
    with pytest.raises(ValueError, match="has not the kit installed"):
        build_review_service(kit, kit.review_world, agent=other, **ports)
    kit.close()


async def test_the_coordinator_stand_in_calls_execute_on_the_kit_agent_and_nothing_else(monkeypatch):
    tripwires(monkeypatch)
    seen: List[AgentMessage] = []

    class Agent:
        async def execute(self, message: AgentMessage) -> AgentResponse:
            seen.append(message)
            return AgentResponse.success(message.task_id, "kit-agent", {"outcomes": []})

        async def process(self, message: AgentMessage) -> AgentResponse:
            raise AssertionError("process() was called")

    coordinator = KitCoordinator(Agent())
    message = AgentMessage.create(
        sender="s", recipient=f"consolidation_agent_{USER}", intent=AgentIntent.DELEGATE, payload={"task": "x"},
        context={"user_id": USER}, timeout_ms=5000,
    )
    stranger = AgentMessage.create(
        sender="s", recipient="smart_agent", intent=AgentIntent.DELEGATE, payload={}, context={"user_id": USER},
    )

    assert (await coordinator.route_message(message)).status is AgentStatus.SUCCESS
    refused = await coordinator.route_message(stranger)

    assert seen == [message] and refused.status is AgentStatus.FAILED and coordinator.routed == [message, stranger]


async def test_the_coordinator_stand_in_holds_the_agent_to_the_messages_timeout():
    class Slow:
        async def execute(self, message: AgentMessage) -> AgentResponse:
            await asyncio.sleep(5)
            raise AssertionError("not reached")

    message = AgentMessage.create(
        sender="s", recipient=f"consolidation_agent_{USER}", intent=AgentIntent.DELEGATE, payload={},
        context={"user_id": USER}, timeout_ms=20,
    )

    with pytest.raises(asyncio.TimeoutError):
        await KitCoordinator(Slow()).route_message(message)


# ---------------------------------------------------------------------------------------------- the review bench

TALK = cases.stale_cases(NOW)[0]
UNDECIDABLE = next(c for c in cases.stale_cases(NOW) if c.key == "undecidable")


TALK_HISTORY = (
    "Alex planned to give a talk at the Hamburg conference on 15 October 2026; whether it took place is unknown."
)


def review_script(*extra: ToolCall):
    """The model: rewrite the plan as history on its passed date (P2 revised: a passed date backs a history rewrite, no
    archive), ask about the undecidable fact (plus `extra`), then report."""
    history = ToolCall(name="update_fact", args={
        "fact_id": TALK.fact_id, "as_history": True,
        "updates": {"state": "current", "content": TALK_HISTORY, "expiry": {"kind": "none"}},
        "basis": {"kind": "date_passed", "date": "2026-10-15"},
    })
    ask = ToolCall(name="ask_owner", args={"fact_id": UNDECIDABLE.fact_id, "fallback": "revive"})
    return metered(calls(history, ask, *extra))


async def seed_live(world: ReviewWorld) -> None:
    """The owner's live memory: two current facts with a horizon, and one fact the janitor staled before S4."""
    for text in ("walks daily", "reads at night"):
        await world.seed(text, expires_at=NOW + timedelta(days=20))
    await world.seed_stale("old legacy note")


async def whole_run(world: ReviewWorld, tmp_path, monkeypatch, capsys, *, stage: int = 2, mirror=None):
    handles, args, live = await review_rig(world, tmp_path, monkeypatch, review_script(), answered())
    runs: List[Dict[str, Any]] = []
    async with s3_bench_support.kit_session(
        handles, args, USER, ACC, "review", with_review=True, summary=support.review_summary,
    ) as (kit, extra):
        run = await support.run_once(
            kit, handles, live, user_id=USER, account_id=ACC, requested=stage, mirror=mirror, clock=CLOCK,
        )
        runs.append(run)
        extra["runs"] = runs
    return kit, run, capsys.readouterr().out


async def test_a_whole_review_bench_run_writes_nothing_live_and_never_reaches_the_real_coordinator(
    tmp_path, monkeypatch, capsys,
):
    world = ReviewWorld()
    tripwires(monkeypatch)
    await seed_live(world)
    before = live_state(world)
    posted_before = list(world.interactive.posted)
    assert before                                                          # the live side has something to protect

    kit, run, out = await whole_run(world, tmp_path, monkeypatch, capsys)

    assert run["result"] == {"status": "completed"}
    assert live_state(world) == before                                     # facts, questions, state, sessions, counters
    assert world.interactive.posted == posted_before and world.enqueued == [] and world.sink.texts == []
    assert world.channels.sent == []                                       # no chat post, no digest
    assert kit.repo.writes and run["stand_ins"]["posts"]                   # the run did write: into the kit


async def test_the_review_bench_yields_the_task_and_label_its_gate_reads(tmp_path, monkeypatch, capsys):
    world = ReviewWorld()
    tripwires(monkeypatch)
    routed: List[AgentMessage] = []
    original = KitCoordinator.route_message

    async def recording(self, message):
        routed.append(message)
        return await original(self, message)

    monkeypatch.setattr(KitCoordinator, "route_message", recording)
    await seed_live(world)

    kit, run, out = await whole_run(world, tmp_path, monkeypatch, capsys)

    assert support.REVIEW_TASK == Intent.REVIEW_STALE_FACTS == "review_stale_facts"
    (message,) = routed
    assert message.payload["task"] == support.REVIEW_TASK and message.recipient == f"consolidation_agent_{USER}"
    assert [p["pass"] for p in kit.passes] == [support.REVIEW_PASS] == ["review"]
    assert kit.passes[0]["llm_calls"] <= support.G5_MAX_TURNS
    assert "G5 ok: 1 review pass(es)" in out


async def test_the_mirror_run_forces_mirror_mode_and_the_summary_compares_the_reported_language(
    tmp_path, monkeypatch, capsys,
):
    world = ReviewWorld()
    tripwires(monkeypatch)
    await seed_live(world)

    kit, run, out = await whole_run(world, tmp_path, monkeypatch, capsys, mirror="ru")

    assert run["owner"]["mirror"] is True and run["owner"]["fixed_language"] is None
    assert run["message_language"] is None                                  # the scripted model reported none
    assert "mirror run 1: reported language None (expected 'ru')" in out


async def test_the_run_stages_the_cases_in_the_overlay_decides_them_and_queues_the_ask(tmp_path, monkeypatch, capsys):
    world = ReviewWorld()
    tripwires(monkeypatch)
    await seed_live(world)
    kit, run, out = await whole_run(world, tmp_path, monkeypatch, capsys)

    plan = kit.repo.overlay_document(TALK.fact_id)
    asked = kit.repo.overlay_document(UNDECIDABLE.fact_id)
    # P2 revised (P2-1): the plan is closed as superseded by its history version, a current fact in the same lineage
    assert plan.state is FactState.SUPERSEDED
    history = kit.repo.overlay_document(plan.superseded_by)
    assert (history.state, history.text, history.lineage_id) == (FactState.CURRENT, TALK_HISTORY, plan.lineage_id)
    assert (asked.state, asked.stale_since) == (FactState.CURRENT, None)        # queued: current while it waits
    assert asked.lifecycle_events[-1]["reason"].startswith(queued_reason(""))
    assert run["batch"] == min(REVIEW_BATCH, run["legacy"] + run["live_staged"] + len(run["cases"]))
    assert run["legacy"] == 1 and run["live_staged"] == 2 and len(run["cases"]) == 8
    assert run["unbased_revives"] == []
    assert {row["case"] for row in run["outcomes"]} >= {TALK.key, UNDECIDABLE.key}
    assert "G5 ok: 0 revive(s) without evidence or a reply" in out
    assert not any(w["kind"] == "add_fact" for w in kit.repo.writes)                  # seeding is not a write


async def test_the_three_reply_cases_are_asked_answered_and_unread_when_the_run_begins(tmp_path, monkeypatch, capsys):
    world = ReviewWorld()
    tripwires(monkeypatch)
    handles, args, live = await review_rig(world, tmp_path, monkeypatch, answered())
    kit = DryRunKit(ledger_path=tmp_path / "spend.json", with_review=True)
    kit.install(handles.agent)
    bundle = kit.review_stand_ins(account_repo=handles.account_repo, user_repo=handles.user_repo, clock=CLOCK)
    bundle.store.seed_state(FactReviewState(account_id=ACC, owner_user_id=USER))

    staged = await support.stage_inputs(kit, bundle, user_id=USER, account_id=ACC, requested=0, now=CLOCK(), channel="D1")

    stored = {q.fact_id: q for q in bundle.store.questions()}
    assert sorted(staged.questions) == ["reply_still_true", "reply_stopped", "reply_unrelated"]
    for case in cases.reply_cases(NOW):
        question = stored[case.fact_id]
        fact = kit.repo.overlay_document(case.fact_id)
        assert (question.status, question.reply_text, question.reply_not_shown) == (QuestionStatus.ASKED, case.reply, True)
        assert question.quoted_text == case.text and question.session_id == f"{USER}:D1"
        assert (fact.state, fact.stale_since) == (FactState.CURRENT, None)
    assert [w["kind"] for w in kit.repo.writes].count("add_fact") == 0
    kit.close()


async def test_a_closed_question_is_seeded_explicitly_and_the_open_loader_does_not_bring_it(tmp_path):
    world = ReviewWorld()
    await world.seed_question(question_id="open-1", fact_id="f1", status=QuestionStatus.QUEUED)
    from scripts.consolidation.review_kit_store import RecordingReviewStore
    from tests.support.review_rule_builders import question as build_question

    kit_store = RecordingReviewStore()
    closed = build_question(QuestionStatus.CLOSED, question_id="closed-1", fact_id="f2")

    await kit_store.load_account(world.review_store, "acc")
    kit_store.seed_question(closed)
    kit_store.seed_state(FactReviewState(account_id="acc"))

    assert sorted(q.question_id for q in kit_store.questions()) == ["closed-1", "open-1"]
    assert kit_store.state("acc") == FactReviewState(account_id="acc")
    assert kit_store.writes == []                                                         # seeding is not a write


async def test_seeding_a_document_in_the_overlay_is_not_a_write_and_has_no_live_rank(store):
    from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
    from tests.unit.scripts.test_s3_dry_run_kit_review_reads import GuardedRepo

    repo = NoWriteFactRepository(GuardedRepo(store.repo))
    entity = cases.fact_entity(TALK, ACC, USER)

    repo.seed_document(entity)

    assert repo.writes == []
    assert (await repo.get_fact_by_id(entity.id)).text == TALK.text
    assert entity.vector is None and entity.expires_at == TALK.expires_at < NOW


# ---------------------------------------------------------------------------------------------- staging helpers

def test_the_batch_never_drops_a_seeded_case_for_a_live_fact():
    fixed = len(cases.stale_cases(NOW)) + len(cases.reply_cases(NOW))

    assert support.live_staged_count(18, 2, fixed) == REVIEW_BATCH - 2 - fixed
    assert support.live_staged_count(3, 2, fixed) == 3
    assert support.live_staged_count(18, 30, fixed) == 0


def test_staged_live_facts_are_the_soonest_due_with_an_expiry_and_never_a_directive():
    from tests.support.review_rule_builders import fact

    base = NOW
    pool = [
        fact("a", state=FactState.CURRENT, expires_at=base + timedelta(days=9)),
        fact("b", state=FactState.CURRENT, expires_at=base + timedelta(days=3)),
        fact("c", state=FactState.CURRENT, expires_at=None),
        fact("d", state=FactState.CURRENT, expires_at=base + timedelta(days=1), domain=FactDomain.AGENT_DIRECTIVE),
        fact("e", state=FactState.CURRENT, expires_at=base + timedelta(days=2)),
    ]

    assert [f.id for f in support.pick_staged(pool, 2)] == ["e", "b"]
    assert [f.id for f in support.pick_staged(pool, 5, skip={"e"})] == ["b", "a"]
    assert support.pick_staged(pool, 0) == []


def test_each_case_went_stale_after_its_horizon_ran_out_and_a_fake_clock_before_the_plan_is_refused():
    for case in (*cases.stale_cases(NOW), *cases.reply_cases(NOW)):
        assert cases.stale_moment(case, NOW) >= case.expires_at
        assert cases.fact_entity(case, ACC, USER).expires_at == case.reported + timedelta(days=case.ttl_days)
    with pytest.raises(ValueError, match="was not due"):
        cases.stale_moment(TALK, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len({c.fact_id for c in (*cases.stale_cases(NOW), *cases.reply_cases(NOW))}) == 8


@pytest.mark.parametrize("days_later", [0, 3, 30, 365, 5 * 365])
def test_a_run_on_any_later_day_stages_every_case_without_raising(days_later):
    # The plan's date is fixed in the text; the owner may run the bench any day after the build.
    later = NOW + timedelta(days=days_later)

    for case in (*cases.stale_cases(later), *cases.reply_cases(later)):
        assert cases.stale_moment(case, later) >= case.expires_at
        assert cases.stale_moment(case, later) < later


async def test_the_review_bench_prints_counts_and_no_fact_text(tmp_path, monkeypatch, capsys):
    world = ReviewWorld()
    tripwires(monkeypatch)
    await seed_live(world)

    kit, run, out = await whole_run(world, tmp_path, monkeypatch, capsys)

    assert "G5" in out                                                        # the summary did print
    for case in (*cases.stale_cases(NOW), *cases.reply_cases(NOW)):
        assert case.text not in out
        assert case.reply is None or case.reply not in out
    for text in ("walks daily", "reads at night", "old legacy note"):
        assert text not in out
    for question in run["question_texts"]:
        assert question not in out


async def test_the_owners_channel_and_language_are_read_live_and_mirror_forces_the_fixed_language_off(tmp_path, monkeypatch):
    world = ReviewWorld()
    handles, _, live = await review_rig(world, tmp_path, monkeypatch, answered())
    kit = DryRunKit(ledger_path=tmp_path / "spend.json", with_review=True)
    kit.install(handles.agent)
    bundle = kit.review_stand_ins(account_repo=Accounts(), user_repo=Users(), clock=CLOCK)

    class Fixed:
        async def get_user(self, user_id: str):
            from src.domain.language import LanguageCode
            return UserProfile(user_id=user_id, account_id=ACC, config=UserBotConfig(
                timezone=LISBON, preferred_language=LanguageCode.UK, agent_mirror=False))

    fixed = await support.seed_owner(bundle, user_id=USER, user_repo=Fixed(), live_state=live)
    assert (await bundle.notification_state.get_primary(USER)).channel_id == "D1"
    assert (fixed["mirror"], fixed["fixed_language"]) == (False, "uk")
    assert (await bundle.language.get_preference(USER))[1] is False

    mirrored = await support.seed_owner(bundle, user_id=USER, user_repo=Fixed(), live_state=live, mirror="ru")
    assert (mirrored["mirror"], mirrored["fixed_language"], mirrored["mirror_expected"]) == (True, None, "ru")
    assert await bundle.language.get_preference(USER) == (None, True)

    class Nobody:
        async def get_primary(self, user_id: str):
            return None

    unseeded = await support.seed_owner(
        kit.review_stand_ins(account_repo=Accounts(), user_repo=Users()), user_id="ann", user_repo=Users(), live_state=Nobody(),
    )
    assert unseeded["channel"] is None
    kit.close()


def test_a_review_that_names_no_basis_for_a_revive_is_counted():
    based = {"actor": "review", "to": "current", "reason": "revived on abcd1234"}
    queue = {"actor": "review", "to": "current", "reason": queued_reason("q1")}
    bare = {"actor": "review", "to": "current", "reason": "looked fine"}
    owner = {"actor": "owner", "to": "current", "reason": "looked fine"}

    assert support.unbased_revives([based, queue, owner, bare]) == ["looked fine"]


def test_the_estimate_follows_plan_8s_two_reference_batches():
    # Reviewer ruling (T12b, F1): the banner gives the upper figure of plan section 8's ranges.
    assert support.estimate_usd(2, 1) == {"sonnet": 0.10, "opus": 0.20}
    assert support.estimate_usd(20, 1) == {"sonnet": 0.40, "opus": 0.80}
    assert support.estimate_usd(20, 2)["opus"] == 1.60


def test_the_review_gate_lines_flag_a_long_pass_an_abandoned_refusal_and_a_stopped_run():
    report = {"passes": [{"pass": "review", "llm_calls": 14, "abandoned_refusals": 1, "born_due": 2},
                         {"pass": "stage1", "llm_calls": 99, "abandoned_refusals": 9, "born_due": 9}],
              "spend": {"aborted": True}}
    extra = {"runs": [{"unbased_revives": ["x"], "owner": {"mirror_expected": "ru"}, "message_language": "uk",
                       "label_languages": []}]}

    lines = support.review_summary(report, extra)

    assert any(l.startswith("G5 FAIL") and "most model turns 14" in l for l in lines)
    assert any(l.startswith("G5 FAIL: 1 abandoned") for l in lines)
    assert any("born-due refusals reported 2" in l for l in lines)
    assert any(l.startswith("G5 FAIL: 1 revive") for l in lines)
    assert any("reported language 'uk' (expected 'ru')" in l for l in lines)
    assert any("RUN STOPPED" in l for l in lines)
    assert support.review_summary({"passes": []}, {})[0].startswith("G5: no review pass ran")


def test_every_bench_prints_that_it_spends_money_and_writes_nothing_live_before_anything_else():
    args = SimpleNamespace(runs=2, stage=18)
    for lines in (review_bench.header(args), support.cost_banner("T", "x")):
        text = "\n".join(lines)
        assert "PAID MODEL CALLS" in text and "NOTHING live" in text and "scripts/memory/" in text
    assert "$" in "\n".join(review_bench.header(args))


def test_the_benches_need_the_kit_flag_and_build_nothing_at_import():
    for module in (review_bench, answers_bench):
        with pytest.raises(SystemExit):
            module.main([])                                                    # --s3-kit missing
        parser = module.build_parser()
        assert parser.parse_args(["--s3-kit", "--user-id", "u", "--account-id", "a"]).s3_kit is True
    parsed = review_bench.build_parser().parse_args(["--s3-kit", "--stage", "4", "--runs", "1", "--mirror", "ru"])
    assert (parsed.stage, parsed.runs, parsed.mirror) == (4, 1, "ru")


# ---------------------------------------------------------------------------------------------- the answer bench

def test_every_answer_window_is_built_from_the_production_shapes():
    for case in cases.answer_cases():
        qid = case.question_id
        if case.name == "echo_control":
            assert qid is None and case.window[0][0] == "model"
            continue
        assert qid
        assert case.window[0] == ("user", review_frame(qid))
        quoted = queued_question(
            cases.FactEntity(account_id="a", created_by_user_id="u", lineage_id="L", text=cases.QUOTED,
                             type=cases.FactType.STATE, state=FactState.STALE, stale_since=NOW),
            qid, "u", "revive", at=NOW,
        )
        if case.name == "correction_translated_quote":   # the uk production shape: uk texts, the stored fact translated
            uk_labels = {key: UK_STRINGS[key] for key in LABEL_KEYS}
            translation = QuoteTranslation("uk", quote_digest(cases.QUOTED), cases.QUOTED_UK)
            expected = fact_question_message(quoted, uk_labels, translation, "uk")
        else:
            expected = fact_question_message(quoted, CANONICAL_LABELS)
        assert case.window[1] == ("model", question_text(expected))
    by_name = {c.name: c for c in cases.answer_cases()}
    assert "correction_translated_quote" in by_name
    assert ("user", reply_part(by_name["digit_reply"].question_id, "2")) in by_name["digit_reply"].window
    assert ("user", not_delivered_frame(by_name["not_delivered"].question_id)) in by_name["not_delivered"].window
    ids = [c.question_id for c in cases.answer_cases() if c.question_id]
    assert len(set(ids)) == len(ids) == len(cases.answer_cases()) - 1


def test_the_judge_reads_no_write_and_one_create():
    none, one = cases.AnswerCase("a", (), cases.NO_WRITE), cases.AnswerCase("b", (), cases.ONE_CREATE, contains="Iron Temple")

    assert cases.judge(none, [])[0] and not cases.judge(none, [{"action": "create", "text": "x"}])[0]
    assert cases.judge(one, [{"action": "create", "text": "Alex trains at the Iron Temple gym"}])[0]
    assert not cases.judge(one, [{"action": "create", "text": "Alex trains at a gym"}])[0]
    assert not cases.judge(one, [{"action": "create", "text": "Iron Temple"}, {"action": "update", "text": "FitLife"}])[0]
    assert not cases.judge(one, [])[0]


def test_a_window_carries_the_roles_texts_and_instants_stage_1_reads():
    case = cases.answer_cases()[0]

    window = cases.window_messages(case, NOW)

    assert [(m["role"], m["text"]) for m in window] == list(case.window)
    assert all(m["timestamp"] == m["created_at"] for m in window)
    assert window[-1]["timestamp"] < NOW.timestamp() and window == sorted(window, key=lambda m: m["timestamp"])


async def test_the_answer_bench_runs_a_case_through_the_real_stage_1_and_judges_the_writes(store, tmp_path):
    agent, kit = await kit_agent(
        store, tmp_path, metered(calls(create("Alex trains at the Iron Temple gym"))), answered(), answered(), answered(),
    )
    kit.timezone = "UTC"
    handles = SimpleNamespace(agent=agent)
    args = SimpleNamespace(user_id=USER, account_id=ACC)
    case = next(c for c in cases.answer_cases() if c.name == "correction")

    result = await answers_bench.run_case(kit, handles, case, [], args)

    message = answers_bench.build_message(kit, case, [], USER, ACC, NOW)
    assert message.payload["task"] == answers_bench.ANSWER_TASK == "consolidate"
    assert [p["pass"] for p in kit.passes][:1] == [answers_bench.ANSWER_PASS] == ["stage1"]
    assert (result["ok"], [w["action"] for w in result["writes"]]) == (True, ["create"])
    assert "Iron Temple" in result["writes"][0]["text"]
    assert result["window"][0]["role"] == "user"


async def test_a_write_where_none_belongs_fails_the_case_and_the_summary_names_it(store, tmp_path):
    # The failure the case exists for: the quoted fact re-created from a digit reply.
    agent, kit = await kit_agent(store, tmp_path, metered(calls(create(cases.QUOTED))), answered(), answered(), answered())
    handles = SimpleNamespace(agent=agent)
    args = SimpleNamespace(user_id=USER, account_id=ACC)
    case = next(c for c in cases.answer_cases() if c.name == "digit_reply")

    result = await answers_bench.run_case(kit, handles, case, [], args)
    lines = answers_bench.answers_summary({"spend": {"aborted": False}}, {"cases": [result]})

    assert result["ok"] is False and "1 fact write" in result["why"]
    assert lines[0] == "G4 FAIL: 0 of 1 case(s) match their rule" and lines[1].startswith("  FAIL digit_reply")
    assert answers_bench.answers_summary({}, {"cases": []})[0] == "G4 ok: 0 of 0 case(s) match their rule"


async def test_an_answer_bench_run_writes_nothing_live(store, tmp_path):
    before = {name: (copy.deepcopy(col.docs), dict(col.versions)) for name, col in store.db.cols.items() if col.docs}
    agent, kit = await kit_agent(store, tmp_path, metered(calls(create("Alex trains at the Iron Temple gym"))), answered())
    handles = SimpleNamespace(agent=agent)
    args = SimpleNamespace(user_id=USER, account_id=ACC)

    for case in cases.answer_cases():
        await answers_bench.run_case(kit, handles, case, [], args)

    assert {name: (col.docs, dict(col.versions)) for name, col in store.db.cols.items() if col.docs} == before


def test_the_duplicate_cases_are_recorded_after_the_surgery_they_describe():
    by_key = {c.key: c for c in cases.stale_cases(NOW)}
    surgery = datetime(2026, 10, 5, tzinfo=timezone.utc)

    assert by_key["duplicate_a"].reported > surgery and by_key["duplicate_b"].reported > surgery
    assert by_key["duplicate_a"].reported < by_key["duplicate_b"].reported
    assert by_key["duplicate_a"].ttl_days == by_key["duplicate_b"].ttl_days == 10


async def test_a_failed_answer_run_shows_fixed_words_and_not_the_agents_error(store, tmp_path, monkeypatch):
    agent, kit = await kit_agent(store, tmp_path, answered())
    handles = SimpleNamespace(agent=agent)
    args = SimpleNamespace(user_id=USER, account_id=ACC)

    async def failing(*a: Any, **k: Any):
        raise s3_bench_support.KitRunFailed("Alex trains at the FitLife gym")

    monkeypatch.setattr(answers_bench, "execute", failing)
    result = await answers_bench.run_case(kit, handles, cases.answer_cases()[0], [], args)

    assert result["ok"] is False and result["why"] == "the run failed"
