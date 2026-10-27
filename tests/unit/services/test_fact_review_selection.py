"""`FactReviewService.run`: selection, prefetch, routing and the queue write (Facts S4 · T8b, plan §3.2 steps 8, 10, 11, 15;
§3.4, §3.14, §3.17).

The plain service on the review world: the real review store, lease, counters, session store, task queue and repository
over one evaluating Firestore fake, a real `FactAnswerService`, `FactQuestionTexts` and `OwnerTimezoneResolver`. The
coordinator is a scripted consolidation agent: it decodes the routed task, plays the pass's tool calls through a real
`FactReviewScope` on the world (the real write path) and answers in the agent's own result form. Assertions are on the
stored facts, questions, state, counters, tasks and the routed message."""
import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import pytest

from src.domain.agent import AgentIntent, AgentResponse
from src.domain.billing import BillingAccount
from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_lifecycle import REVIEW_ACCEPTS
from src.domain.fact_review_language import labels_digest
from src.domain.fact_review_question import ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.fact_review_reasons import queued_reason
from src.domain.fact_review_result import FactReviewResult, encode_review_result
from src.domain.fact_review_rules import revives_in_window
from src.domain.fact_review_task import FactReviewTask, decode_review_task
from src.domain.fact_review_task_keys import defer_key
from src.domain.language import LanguageCode
from src.domain.llm import Message, MessagePart
from src.domain.request_context import get_current_account_id, get_current_user_id
from src.domain.ui_messages import UIMessage
from src.domain.user import UserBotConfig, UserProfile
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.fact_review_scope import FactReviewScope
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_lifecycle_sweep_service import FactLifecycleSweepService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.fact_review_service import FactReviewService, _Timeouts
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_p2_labels import PT_P2_LABELS
from tests.support.review_fact_updates import RecordingFactUpdates
from tests.support.review_rule_builders import question as build_question

USER = "u1"
TZ = "Europe/Lisbon"            # the world's NOW, 07:30 UTC, is 08:30 the same day in the owner's zone
SESSION = f"{USER}:D1"
HORIZON = {"kind": "horizon", "days": 30}
REVIVE = {"state": "current", "expiry": HORIZON}

Play = Callable[[FactReviewTask, FactReviewScope], Awaitable[None]]

PT_LABELS = {
    UIMessage.MEMORY_REVIEW_QUESTION.value: "Isto ainda é verdade?",
    UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Sim",
    UIMessage.MEMORY_OPTION_ENDED.value: "Já não",
    UIMessage.MEMORY_OPTION_NEVER.value: "Nunca",
    UIMessage.MEMORY_OPTION_DONT_KNOW.value: "Não me lembro",
    UIMessage.MEMORY_REPLY_HINT.value: "Responda a esta mensagem com um algarismo ou por palavras.",
    UIMessage.MEMORY_ANSWER_NOTED.value: "Obrigado, registado.",
    UIMessage.MEMORY_REPLY_NOTED.value: "Anotado, vou ter isso em conta.",
    UIMessage.MEMORY_ALREADY_ANSWERED.value: "Já respondido, obrigado.",
    UIMessage.MEMORY_FACT_CHANGED.value: "Esse facto mudou entretanto, por isso não apliquei a resposta; voltarei a ele.",
    UIMessage.MEMORY_QUESTION_CLOSED.value: "Esta pergunta está fechada.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Factos que pus de lado esta semana:",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "Para recuperar um, diga-me no chat que ainda é verdade.",
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "já não é verdade",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "nunca foi verdade",
    **PT_P2_LABELS,  # P2 revised (C11): the set is the 28 label keys
}


class Accounts:
    def __init__(self) -> None:
        self.held = {ACC: BillingAccount(account_id=ACC, iam_policy={USER: "owner"})}

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        return self.held.get(account_id)


class Users:
    def __init__(self) -> None:
        self.held = {USER: UserProfile(user_id=USER, account_id=ACC, config=UserBotConfig(timezone=TZ))}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self.held.get(user_id)


class Agents:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


class ScriptedAgent:
    """The consolidation agent behind the coordinator. It reads the task from the routed message, runs `play` over a
    real `FactReviewScope` (the pass's `update_fact` / `ask_owner` through the real write path) and answers with the
    scope's outcomes, asks and kept facts in the agent's result form; in mirror mode with `language` and `labels`.
    `raises` makes the coordinator raise instead. `seen` holds the request context each call ran in."""

    def __init__(self, world: ReviewWorld) -> None:
        self.world = world
        self.routed: List[Any] = []
        self.tasks: List[FactReviewTask] = []
        self.seen: List[tuple] = []
        self.play: Optional[Play] = None
        self.language: Optional[str] = None
        self.labels: Optional[Dict[str, str]] = None
        self.raises: Optional[Exception] = None

    async def route_message(self, message: Any) -> AgentResponse:
        self.routed.append(message)
        self.seen.append((get_current_user_id(), get_current_account_id()))
        if self.raises is not None:
            raise self.raises
        task = decode_review_task(message.payload)
        self.tasks.append(task)
        scope = FactReviewScope(task.batch, message.context["account_id"], message.context["owner_timezone"],
                                self.world.now, self.world.repo, self.world.store.adapter)
        if self.play is not None:
            await self.play(task, scope)
        result = FactReviewResult(
            outcomes=tuple(scope.decisions()), asks=tuple(scope.asks()), kept=tuple(f.id for f in scope.kept()),
            message_language=self.language if task.mirror else None, labels=self.labels if task.mirror else None,
        )
        return AgentResponse.success(message.task_id, message.recipient, encode_review_result(result))


class Rig:
    def __init__(self, world: ReviewWorld, clock: Optional[Callable[[], datetime]] = None,
                 service_class: type = FactReviewService) -> None:
        self.world = world
        self.agent = ScriptedAgent(world)
        accounts, users = Accounts(), Users()
        zones = OwnerTimezoneResolver(accounts, users)
        texts = FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                  LanguageCode.EN, clock=world.clock)
        answers = FactAnswerService(
            fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
            task_queue=world.queue, session_store=world.session_store, texts=texts, interactive=world.interactive,
            owner_zones=zones, clock=world.clock,
        )
        self.service = service_class(
            repo=world.repo, review_store=world.review_store, counters=world.counters, lease=world.lease,
            coordinator=self.agent, agent_factory=Agents(), user_repo=users, account_repo=accounts,
            texts=texts, owner_zones=zones, notification_state=world.notification_state,
            channel_bindings=world.channel_bindings, notifications=world.notifier, session_store=world.session_store,
            task_queue=world.queue, answer_service=answers, interactive=world.interactive, alert_sink=world.sink,
            run_timeout_s=600, clock=clock or world.clock,
            fact_updates=RecordingFactUpdates(world.store.adapter), cabinet_url="https://cabinet.example/cabinet?tab=memory",
        )

    async def run(self) -> Dict[str, Any]:
        return await self.service.run(USER, ACC, "daily")


# ------------------------------------------------------------------------------------------------ seeding and reading

async def stale(world: ReviewWorld, text: str, stale_since: Optional[datetime] = None, **over: Any) -> str:
    """A horizon fact reported 60 days ago that the janitor staled; `stale_since` set to the given moment (None: none)."""
    over.setdefault("expiry_kind", ExpiryKind.HORIZON)
    over.setdefault("ttl_days", 14)
    over.setdefault("reported_date", NOW - timedelta(days=60))
    fact_id = await world.seed_stale(text, **over)
    world.docs[fact_id]["stale_since"] = stale_since
    return fact_id


async def evidence(world: ReviewWorld, text: str) -> str:
    return await world.seed(text, reported_date=NOW - timedelta(days=1))


async def current(world: ReviewWorld, text: str) -> str:
    """A current horizon fact, still within its window."""
    return await world.seed(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=60, reported_date=NOW - timedelta(days=40),
                            expires_at=NOW + timedelta(days=20))


async def asked(world: ReviewWorld, question_id: str, fact_id: str, **over: Any) -> FactReviewQuestion:
    text = world.docs[fact_id]["text"]
    over.setdefault("asked_at", NOW - timedelta(hours=2))
    return await world.seed_question(QuestionStatus.ASKED, question_id=question_id, account_id=ACC, user_id=USER,
                                     fact_id=fact_id, quoted_text=text, **over)


def on(fact_id: str) -> Dict[str, Any]:
    return {"kind": "evidence", "evidence_fact_id": fact_id}


async def stored_question(world: ReviewWorld, question_id: str) -> FactReviewQuestion:
    return (await world.review_store.get_questions([question_id]))[0]


async def open_questions(world: ReviewWorld) -> List[FactReviewQuestion]:
    return await world.review_store.questions_for_account(ACC)


async def day_row(world: ReviewWorld):
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0] if rows else None


def batch_ids(task: FactReviewTask) -> List[str]:
    return [item.fact.id for item in task.items]


def today() -> date:
    return NOW.date()


async def ask_all(task: FactReviewTask, scope: FactReviewScope) -> None:
    for item in task.items:
        result = await scope.ask_owner({"fact_id": item.fact.id, "fallback": "revive"})
        assert result["status"] == "asked", result


# ------------------------------------------------------------------------------------------------ step 8: selection

class TestSelection:
    async def test_the_batch_is_replies_first_then_stale_by_offers_and_age_and_an_open_question_holds_its_fact(self):
        world = ReviewWorld()
        old = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=9))
        newer = await stale(world, "Alex reads at the Example Library", NOW - timedelta(days=2))
        undated = await stale(world, "Alex plans a weekend in Coimbra", None)
        offered_once = await stale(world, "Alex walks to Main Square", NOW - timedelta(days=30))
        held = await stale(world, "Alex visits Clínica Ribeira", NOW - timedelta(days=40))
        kept_twice = await stale(world, "Alex orders a cookbook", NOW - timedelta(days=50))
        await world.seed_question(QuestionStatus.UNANSWERED, question_id="qh", account_id=ACC, fact_id=held,
                                  quoted_text="Alex visits Clínica Ribeira", asked_at=NOW - timedelta(days=2))
        replied = await current(world, "Alex trains at the Iron Temple gym")
        await asked(world, "qr", replied, reply_text="yes, still there every Tuesday", reply_at=NOW - timedelta(hours=1))
        await world.seed_state(account_id=ACC, offers={offered_once: 1, kept_twice: 2},
                               current_question_id="qr", sent_on=today(), sent=1)
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        (task,) = r.agent.tasks
        assert batch_ids(task) == [replied, undated, old, newer, offered_once]
        assert held not in batch_ids(task) and kept_twice not in batch_ids(task)     # the question's; T8c's
        assert [q.question_id for q in task.questions] == ["qr"]
        assert task.questions[0].reply_text == "yes, still there every Tuesday"
        state = await world.review_store.get_state(ACC)
        # the reply fact is no offer; step 7 queued the kept-twice fact and removed its entry (S4-D20)
        assert dict(state.offers) == {old: 1, newer: 1, undated: 1, offered_once: 2}
        assert world.docs[kept_twice]["state"] == "current"
        assert [(q.fact_id, q.status) for q in await open_questions(world) if q.fact_id == kept_twice] == \
            [(kept_twice, QuestionStatus.QUEUED)]
        assert (await stored_question(world, "qr")).offered_at == NOW                # shown once, before routing
        assert (await stored_question(world, "qh")).status is QuestionStatus.UNANSWERED
        assert world.docs[held]["state"] == "stale"

    async def test_the_batch_is_capped_at_twenty_longest_stale_first(self):
        world = ReviewWorld()
        ids = [await stale(world, f"neutral fact {n:02d}", NOW - timedelta(days=30 - n)) for n in range(22)]
        r = Rig(world)

        await r.run()

        assert batch_ids(r.agent.tasks[0]) == ids[:20]

    async def test_nothing_stale_and_no_reply_makes_no_model_call(self):
        world = ReviewWorld()
        await current(world, "Alex works at Acme")
        r = Rig(world)

        assert await r.run() == {"status": "completed"}
        assert r.agent.routed == []


# ------------------------------------------------------------------------------------------------ step 10: the message

class TestRouting:
    async def test_the_routed_message_has_the_consolidation_shape_and_runs_in_the_owners_request_context(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        r = Rig(world)

        await r.run()

        (message,) = r.agent.routed
        assert message.sender == "fact_review_service"
        assert message.recipient == f"consolidation_agent_{USER}"
        assert message.intent is AgentIntent.DELEGATE
        assert set(message.payload) == {"task", "review"} and message.payload["task"] == Intent.REVIEW_STALE_FACTS
        assert message.context == {"user_id": USER, "account_id": ACC, "owner_timezone": TZ,
                                   "suppress_transient_retry": True}
        assert message.timeout_ms == 600_000                         # the lease left: 600 s run, the margin kept
        assert message.task_id
        assert r.agent.seen == [(USER, ACC)]
        assert batch_ids(r.agent.tasks[0]) == [fact]

    async def test_the_task_carries_the_runs_instant(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        await stale(world, "Alex plans a weekend in Coimbra", NOW - timedelta(days=9))
        r = Rig(world)

        await r.run()

        (task,) = r.agent.tasks
        assert task.now == NOW

    async def test_each_fact_carries_its_earlier_versions_and_five_neighbours_outside_its_lineage_and_no_vector(self):
        world = ReviewWorld()
        first = await world.seed("Alex is a data engineer", lineage_id="lin-job", version=1, state=FactState.SUPERSEDED,
                                 is_current=False)
        later = await stale(world, "Alex works at Acme", NOW - timedelta(days=3), lineage_id="lin-job", version=2)
        for n in range(7):
            await world.seed(f"Alex's colleague note {n}")
        replied = await current(world, "Alex trains at the Iron Temple gym")
        await asked(world, "qr", replied, reply_text="still going", reply_at=NOW - timedelta(hours=1))
        r = Rig(world)

        await r.run()

        items = {item.fact.id: item for item in r.agent.tasks[0].items}
        assert [v.id for v in items[later].versions] == [first]
        assert items[replied].versions == ()
        for item in items.values():
            assert len(item.neighbours) == 5
            assert all(n.lineage_id != item.fact.lineage_id for n in item.neighbours)   # its own document dropped
            assert all(n.state is FactState.CURRENT for n in item.neighbours)
            for fact in (item.fact, *item.versions, *item.neighbours):
                assert (fact.vector, fact.tags_vector, fact.metadata_vector) == (None, None, None)

    async def test_mirror_mode_sends_the_owners_recent_messages_and_stores_the_language_and_checked_labels(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        await world.notification_state.save_primary(USER, "slack", "D1")
        await world.session_store.append_messages_batch(SESSION, [
            Message(role="user", parts=[MessagePart(text="Vou ao ginásio amanhã de manhã")]),
            Message(role="model", parts=[MessagePart(text="Boa!")]),
            Message(role="user", parts=[MessagePart(text="[System: a reminder fired]")]),
            Message(role="user", parts=[MessagePart(text="E depois ao Example Library")]),
        ], owner_id=USER, raise_on_error=True)
        await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        r = Rig(world)
        r.agent.language, r.agent.labels = "PT", dict(PT_LABELS)

        await r.run()

        (task,) = r.agent.tasks
        assert task.mirror is True
        assert task.owner_messages == ("Vou ao ginásio amanhã de manhã", "E depois ao Example Library")
        state = await world.review_store.get_state(ACC)
        assert state.message_language == "pt"
        assert dict(state.labels["pt"]) == PT_LABELS and state.label_digests["pt"] == labels_digest()

    async def test_a_label_set_that_fails_the_check_keeps_the_earlier_one(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC, message_language="pt", labels={"pt": PT_LABELS},
                               label_digests={"pt": labels_digest()})
        await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        r = Rig(world)
        bad = {**PT_LABELS, UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Sim 1"}         # a digit
        r.agent.language, r.agent.labels = "pt", bad

        await r.run()

        state = await world.review_store.get_state(ACC)
        assert state.message_language == "pt" and dict(state.labels["pt"]) == PT_LABELS

    async def test_fixed_mode_sends_no_owner_messages_and_stores_no_language(self):
        world = ReviewWorld()
        world.language.preferences[USER] = (LanguageCode.EN, False)
        await world.seed_state(account_id=ACC)
        await world.notification_state.save_primary(USER, "slack", "D1")
        await world.session_store.append_messages_batch(
            SESSION, [Message(role="user", parts=[MessagePart(text="Vou ao ginásio")])], owner_id=USER, raise_on_error=True)
        await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        r = Rig(world)
        r.agent.language, r.agent.labels = "pt", dict(PT_LABELS)

        await r.run()

        assert r.agent.tasks[0].mirror is False and r.agent.tasks[0].owner_messages == ()
        state = await world.review_store.get_state(ACC)
        assert state.message_language is None and dict(state.labels) == {}


# ------------------------------------------------------------------------------------------------ F2 and delta 11

class TestLeaseBudgetAndFailure:
    async def test_the_agent_timeout_is_the_lease_left_at_routing_time_read_on_a_second_clock_read(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        reads: List[datetime] = []

        def clock() -> datetime:          # the setup and housekeeping took 100 s before routing
            reads.append(NOW + timedelta(seconds=100 * len(reads)))
            return reads[-1]

        r = Rig(world, clock=clock)
        assert await r.run() == {"status": "completed"}

        assert reads == [NOW, NOW + timedelta(seconds=100)]
        assert r.agent.routed[0].timeout_ms == 500_000               # (NOW + 600 + 60) - 60 - (NOW + 100)
        state = await world.review_store.get_state(ACC)
        assert state.started_at == state.last_review_at == NOW       # every mark has the run's one now
        assert (await open_questions(world)) == []

    async def test_less_than_the_margin_left_defers_the_run_and_routes_and_counts_nothing(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        reads: List[datetime] = []

        def clock() -> datetime:          # 550 s went before routing: 50 s left, under the 60 s margin
            reads.append(NOW if not reads else NOW + timedelta(seconds=550))
            return reads[-1]

        r = Rig(world, clock=clock)
        assert await r.run() == {"status": "deferred"}

        assert r.agent.routed == []
        state = await world.review_store.get_state(ACC)
        assert dict(state.offers) == {} and state.last_review_at is None and state.last_failed_at is None
        assert world.enqueued == [("fact_review", {"task_type": "fact_review", "user_id": USER, "account_id": ACC,
                                                   "mode": "daily"})]
        digest = hashlib.sha256(defer_key(ACC, NOW).encode()).hexdigest()[:32]
        assert any(name.endswith(f"/tasks/fact_review-{digest}") for name in world.tasks.names)   # the lease-busy deferral's name
        assert world.docs[fact]["state"] == "stale"
        assert world.store.db.collection(world.env.memory_write_leases_collection).docs.get(ACC) is None

    async def test_a_coordinator_exception_is_the_passs_failure_and_the_run_answers_failed(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        r = Rig(world)
        r.agent.raises = RuntimeError("coordinator down")

        assert await r.run() == {"status": "failed"}

        assert (await day_row(world)).review_failed == 1
        state = await world.review_store.get_state(ACC)
        assert state.last_failed_at == NOW and state.unsuccessful_since == NOW and state.last_review_at is None
        assert dict(state.offers) == {fact: 1}                        # the offer was written before routing; it stands
        assert world.docs[fact]["state"] == "stale"


# ------------------------------------------------------------------------------------------------ step 11

class TestAfterThePass:
    async def test_a_decided_fact_loses_its_offers_entry_a_kept_one_is_counted_and_the_counts_land(self):
        world = ReviewWorld()
        revived = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=9))
        kept = await stale(world, "Alex reads at the Example Library", NOW - timedelta(days=3))
        ev = await evidence(world, "Alex logged the weight again this morning")
        await world.seed_state(account_id=ACC, offers={revived: 1})
        r = Rig(world)

        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            result = await scope.update_fact({"fact_id": revived, "updates": REVIVE, "basis": on(ev)})
            assert result["status"] == "updated", result

        r.agent.play = play
        assert await r.run() == {"status": "completed"}

        state = await world.review_store.get_state(ACC)
        assert dict(state.offers) == {kept: 1}
        assert world.docs[revived]["state"] == "current" and world.docs[kept]["state"] == "stale"
        row = await day_row(world)
        assert (row.reviewed, row.revived, row.kept, row.queued) == (2, 1, 1, 0)

    async def test_a_revive_is_not_staled_by_the_next_days_janitor(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=9))
        ev = await evidence(world, "Alex logged the weight again this morning")
        r = Rig(world)

        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

        r.agent.play = play
        await r.run()
        world.advance(days=1)
        await FactLifecycleSweepService(world.repo, world.counters, None, False, clock=world.clock).sweep()

        assert world.docs[fact]["state"] == "current" and world.docs[fact]["stale_since"] is None
        assert fact not in {due.fact_id for due in await world.repo.find_due_facts(world.now)}

    async def test_an_ask_makes_its_fact_current_and_queues_the_question_in_its_stale_place(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        since = NOW - timedelta(days=9)
        fact = await stale(world, "Alex plans a weekend in Coimbra", since)
        expires_before = world.docs[fact]["expires_at"]
        r = Rig(world)
        r.agent.play = ask_all

        assert await r.run() == {"status": "completed"}

        (question,) = await open_questions(world)
        assert question.status is QuestionStatus.QUEUED and question.fact_id == fact
        assert question.queue_key == since and question.queued_at == NOW
        assert question.quoted_text == "Alex plans a weekend in Coimbra" and question.fallback == "revive"
        doc = world.docs[fact]
        assert doc["state"] == "current" and doc["stale_since"] is None and doc["expires_at"] == expires_before
        event = doc["lifecycle_events"][-1]
        assert (event["from"], event["to"], event["actor"]) == ("stale", "current", "review")
        assert event["reason"] == queued_reason(question.question_id)
        assert (await day_row(world)).queued == 1
        assert dict((await world.review_store.get_state(ACC)).offers) == {}      # queued: no longer an offer

    async def test_a_fact_with_no_stale_since_queues_at_the_front(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        await stale(world, "Alex plans a weekend in Coimbra", None)
        r = Rig(world)
        r.agent.play = ask_all

        await r.run()

        (question,) = await open_questions(world)
        assert question.queue_key == datetime(1970, 1, 1, tzinfo=timezone.utc)

    async def test_an_ask_whose_fact_was_closed_meanwhile_creates_no_question_and_counts_none(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex plans a weekend in Coimbra", NOW - timedelta(days=9))
        r = Rig(world)

        async def play(task, scope):
            await ask_all(task, scope)
            world.docs[fact]["state"] = "archived"         # another writer closes the fact while the pass runs

        r.agent.play = play

        assert await r.run() == {"status": "completed"}

        assert await open_questions(world) == []
        row = await day_row(world)
        assert row is None or row.queued == 0
        assert dict((await world.review_store.get_state(ACC)).offers) == {fact: 1}    # not queued: still an offer

    async def test_a_conflicting_fact_write_leaves_the_fact_stale_and_creates_no_question(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex plans a weekend in Coimbra", NOW - timedelta(days=9))
        r = Rig(world)
        r.agent.play = ask_all
        real = world.repo.update_fact_fields
        raced: List[str] = []

        async def racing(stored: Any, fields: Any) -> None:
            if stored.fact.id == fact and not raced:      # another writer lands between the read and the write
                raced.append(fact)
                await real(await world.repo.resolve_current(fact, ACC, REVIEW_ACCEPTS), {"last_updated": world.now})
            await real(stored, fields)

        world.repo.update_fact_fields = racing  # type: ignore[method-assign]

        assert await r.run() == {"status": "completed"}

        assert raced == [fact]
        assert world.docs[fact]["state"] == "stale" and world.docs[fact]["stale_since"] is not None
        assert await open_questions(world) == []
        row = await day_row(world)
        assert row is not None and row.queued == 0
        assert dict((await world.review_store.get_state(ACC)).offers) == {fact: 1}   # not queued: still an offer

    async def test_twenty_undecidable_facts_are_twenty_queued_questions_and_none_is_revived(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        ids = [await stale(world, f"neutral fact {n:02d}", NOW - timedelta(days=30 - n)) for n in range(20)]
        r = Rig(world)
        r.agent.play = ask_all

        assert await r.run() == {"status": "completed"}

        questions = await open_questions(world)
        assert sorted(q.fact_id for q in questions) == sorted(ids)
        assert all(q.status is QuestionStatus.QUEUED for q in questions)
        for fact_id in ids:
            doc = world.docs[fact_id]
            assert doc["state"] == "current"
            assert revives_in_window(doc["lifecycle_events"], NOW) == 0
        row = await day_row(world)
        assert (row.queued, row.revived, row.reviewed) == (20, 0, 20)
        assert world.sink.texts == []
        assert dict((await world.review_store.get_state(ACC)).offers) == {}

    async def test_a_reply_in_words_is_shown_once_and_the_reviews_outcome_closes_it_and_releases_the_next(self):
        world = ReviewWorld()
        replied = await current(world, "Alex trains at the Iron Temple gym")
        await asked(world, "q1", replied, reply_text="yes, still there every Tuesday", reply_at=NOW - timedelta(hours=1))
        waiting = await current(world, "Alex walks to Main Square")
        await world.seed_question(QuestionStatus.QUEUED, question_id="q2", account_id=ACC, user_id=USER,
                                  fact_id=waiting, quoted_text="Alex walks to Main Square")
        await world.seed_state(account_id=ACC, current_question_id="q1", sent_on=today(), sent=1)
        r = Rig(world)

        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            basis = {"kind": "reply", "question_id": "q1", "quote": "still there every Tuesday"}
            result = await scope.update_fact({"fact_id": replied, "updates": REVIVE, "basis": basis})
            assert result["status"] == "updated", result

        r.agent.play = play
        assert await r.run() == {"status": "completed"}

        closed = await stored_question(world, "q1")
        assert closed.status is QuestionStatus.CLOSED and closed.closed_by is ClosedBy.REVIEW
        assert closed.answer_source == "review" and closed.offered_at == NOW
        assert (await world.review_store.get_state(ACC)).current_question_id is None
        assert ("fact_review", {"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}) \
            in world.enqueued                                         # the next question is released
        assert (await stored_question(world, "q2")).status is QuestionStatus.QUEUED
        row = await day_row(world)
        assert (row.closed_by_reply, row.revived) == (1, 1)

    async def test_a_reply_left_undecided_is_not_shown_again(self):
        world = ReviewWorld()
        replied = await current(world, "Alex trains at the Iron Temple gym")
        await asked(world, "q1", replied, reply_text="hm, maybe", reply_at=NOW - timedelta(hours=1))
        await world.seed_state(account_id=ACC, current_question_id="q1", sent_on=today(), sent=1)
        r = Rig(world)

        await r.run()
        world.advance(hours=1)
        await r.run()

        assert len(r.agent.routed) == 1 and batch_ids(r.agent.tasks[0]) == [replied]
        question = await stored_question(world, "q1")
        assert question.status is QuestionStatus.ASKED and question.offered_at == NOW


# ------------------------------------------------------------------------------------------------ timed-out candidates

class WithCandidates(FactReviewService):
    """The plain service with only the T8c stub `_timeouts` answered: it names the timed-out questions' facts as the
    batch's candidates, as step 6 will."""

    async def _timeouts(self, user_id, account_id, questions, now, zone, writes):
        facts = [await self._repo.get_fact_by_id(q.fact_id) for q in questions if q.is_timed_out(now)]
        return _Timeouts(candidates=tuple(f for f in facts if f is not None))


class TestTimedOutCandidates:
    async def _world(self) -> tuple:
        world = ReviewWorld()
        fact = await current(world, "Alex reads at the Example Library")
        await world.seed_question(QuestionStatus.UNANSWERED, question_id="qt", account_id=ACC, user_id=USER,
                                  fact_id=fact, quoted_text="Alex reads at the Example Library",
                                  asked_at=NOW - timedelta(days=15))
        await world.seed_state(account_id=ACC)
        return world, fact

    async def test_an_undecided_candidate_gets_its_fallback_and_its_question_times_out(self):
        world, fact = await self._world()
        r = Rig(world, service_class=WithCandidates)

        assert await r.run() == {"status": "completed"}

        assert batch_ids(r.agent.tasks[0]) == [fact]
        question = await stored_question(world, "qt")
        assert question.status is QuestionStatus.CLOSED and question.closed_by is ClosedBy.TIMEOUT
        doc = world.docs[fact]
        assert doc["state"] == "current" and doc["lifecycle_events"][-1]["reason"] == "no answer to q=qt: revive"
        assert doc["expires_at"] > NOW + timedelta(days=20)                        # a doubled window from now
        assert (await day_row(world)).timed_out == 1
        state = await world.review_store.get_state(ACC)
        assert dict(state.offers) == {}                                            # a current candidate is no offer

    async def test_a_decided_candidate_is_closed_by_the_review_with_no_fallback(self):
        world, fact = await self._world()
        ev = await evidence(world, "Alex returned the last library book and closed the card")
        r = Rig(world, service_class=WithCandidates)

        # P2 revised (P2-2): a standing fact a neighbour shows ended is superseded by it, never archived on evidence
        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            result = await scope.update_fact({"fact_id": fact, "updates": {"state": "superseded", "superseded_by": ev}})
            assert result["status"] == "updated", result

        r.agent.play = play
        await r.run()

        question = await stored_question(world, "qt")
        assert question.closed_by is ClosedBy.REVIEW and question.answer_source is None
        assert world.docs[fact]["state"] == "superseded"
        row = await day_row(world)
        assert (row.timed_out, row.superseded, row.closed_by_reply) == (0, 1, 0)


# ------------------------------------------------------------------------------------------------ step 15: the revive alert

def revived_on(days_ago: int) -> Dict[str, Any]:
    return {"at": NOW - timedelta(days=days_ago), "from": "stale", "to": "current", "actor": "review",
            "reason": "revived on 0abc1234"}


class TestReviveAlert:
    async def _revive(self, events: Sequence[Dict[str, Any]]) -> tuple:
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=2), lifecycle_events=list(events))
        ev = await evidence(world, "Alex logged the weight again this morning")
        r = Rig(world)

        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

        r.agent.play = play
        assert await r.run() == {"status": "completed"}
        return world, fact

    async def test_a_third_revive_within_thirty_days_raises_one_alert_naming_the_fact(self):
        world, fact = await self._revive([revived_on(20), revived_on(10)])

        assert revives_in_window(world.docs[fact]["lifecycle_events"], NOW) == 3
        assert len(world.sink.texts) == 1
        assert world.sink.texts[0].startswith("🚨") and f"{fact[:8]} (3×)" in world.sink.texts[0]

    @pytest.mark.parametrize("earlier", [[revived_on(20)], [revived_on(45), revived_on(40)]])
    async def test_fewer_revives_within_thirty_days_raise_none(self, earlier):
        world, fact = await self._revive(earlier)

        assert world.docs[fact]["state"] == "current"
        assert world.sink.texts == []

    async def test_a_fallback_revive_that_reaches_the_limit_raises_the_alert(self):
        world = ReviewWorld()
        fact = await world.seed("Alex reads at the Example Library", expiry_kind=ExpiryKind.HORIZON, ttl_days=60,
                                reported_date=NOW - timedelta(days=40), expires_at=NOW + timedelta(days=20),
                                lifecycle_events=[revived_on(20), revived_on(10)])
        await world.seed_question(QuestionStatus.UNANSWERED, question_id="qt", account_id=ACC, user_id=USER,
                                  fact_id=fact, quoted_text="Alex reads at the Example Library",
                                  asked_at=NOW - timedelta(days=15))
        await world.seed_state(account_id=ACC)
        r = Rig(world, service_class=WithCandidates)                  # the pass leaves the candidate undecided

        assert await r.run() == {"status": "completed"}

        assert world.docs[fact]["lifecycle_events"][-1]["reason"] == "no answer to q=qt: revive"
        assert (await day_row(world)).timed_out == 1
        assert len(world.sink.texts) == 1
        assert world.sink.texts[0].startswith("🚨") and f"{fact[:8]} (3×)" in world.sink.texts[0]

    async def test_a_failed_alert_post_does_not_fail_the_run(self):
        world = ReviewWorld()
        world.sink.fail = True
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=2),
                           lifecycle_events=[revived_on(20), revived_on(10)])
        ev = await evidence(world, "Alex logged the weight again this morning")
        r = Rig(world)

        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

        r.agent.play = play
        assert await r.run() == {"status": "completed"}
        assert (await world.review_store.get_state(ACC)).last_review_at == NOW
