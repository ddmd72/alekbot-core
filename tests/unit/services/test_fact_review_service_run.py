"""`FactReviewService.run` — the run skeleton (Facts S4 · T8a, plan §3.2 steps 1-5, 9, 13, 15 and the failure rule).

On the review world: the real review store, memory-write lease, counters, session store, task queue (with Cloud Tasks'
name refusal) and repository over one evaluating Firestore fake, a real `FactAnswerService`, `FactQuestionTexts` and
`OwnerTimezoneResolver`. The later steps are stubs in this task; `Recording` keeps what each stub was given and can script
what it returns. Assertions are on the stored questions, state, lease, session history, counters and created tasks."""
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

from src.domain.agent import AgentResponse
from src.domain.billing import BillingAccount
from src.domain.fact_lifecycle_day import CounterField
from src.domain.fact_review_frame import NOT_DELIVERED_REPLY, not_delivered_frame
from src.domain.fact_review_question import FactReviewQuestion, QuestionStatus
from src.domain.fact_review_state import FactReviewState
from src.domain.fact_review_task_keys import defer_key
from src.domain.language import LanguageCode
from src.domain.messaging import MessageRef
from src.domain.user import UserBotConfig, UserProfile
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.fact_review_service import FactReviewService, _StepResult, _Timeouts
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_fact_updates import RecordingFactUpdates
from tests.support.review_rule_builders import fact as build_fact

ACC = "acc"
USER = "u1"
SESSION = "u1:D1"
TZ = "Europe/Lisbon"   # the world's NOW, 07:30 UTC, is 08:30 on the same day in the owner's zone


class Accounts:
    """The account store: holds accounts by id, records every id asked for."""

    def __init__(self, *held: BillingAccount) -> None:
        self.held = {a.account_id: a for a in held}
        self.asked: List[str] = []

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        self.asked.append(account_id)
        return self.held.get(account_id)


class Users:
    """The user store: holds profiles, records every id asked for."""

    def __init__(self, *held: UserProfile) -> None:
        self.held = {p.user_id: p for p in held}
        self.asked: List[str] = []

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        self.asked.append(user_id)
        return self.held.get(user_id)


class Agents:
    def __init__(self) -> None:
        self.ensured: List[str] = []

    async def ensure_agents_for_user(self, user_id: str) -> None:
        self.ensured.append(user_id)


class Coordinator:
    """Records the routed message and raises. A run that routes runs the real `_route` under a scripted `route`, so
    the raise becomes a failed response that the script's answer then replaces; no test here reads that failure."""

    def __init__(self) -> None:
        self.routed: List[Any] = []

    async def route_message(self, message: Any) -> Any:
        self.routed.append(message)
        raise AssertionError("no model call is made by the run skeleton")


class Recording(FactReviewService):
    """The service with its stubs recorded: `calls` holds (stub, now) in call order. `script` maps a stub to what it
    returns, or to a callable that returns it: a writing step's callable gets the run's write recorder, `select`'s
    gets `now`. Unscripted stubs run as built."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: List[Tuple[str, datetime]] = []
        self.script: Dict[str, Any] = {}

    async def _answer(self, name: str, now: datetime, built: Any, *args: Any) -> Any:
        self.calls.append((name, now))
        scripted = self.script.get(name, built)
        if callable(scripted) and not isinstance(scripted, (_StepResult, AgentResponse)):
            result = scripted(*args)
            return await result if hasattr(result, "__await__") else result
        return scripted

    async def _timeouts(self, user_id, account_id, questions, now, zone, writes):
        built = await super()._timeouts(user_id, account_id, questions, now, zone, writes)
        return await self._answer("timeouts", now, built, writes)

    async def _kept_twice(self, user_id, account_id, state, questions, now, zone, writes):
        built = await super()._kept_twice(user_id, account_id, state, questions, now, zone, writes)
        return await self._answer("kept_twice", now, built, writes)

    async def _select(self, account_id, state, questions, candidates, now):
        return await self._answer("select", now, await super()._select(account_id, state, questions, candidates, now),
                                  now)

    async def _route(self, user_id, account_id, batch, questions, now, zone, until):
        built = await super()._route(user_id, account_id, batch, questions, now, zone, until)
        return await self._answer("route", now, built)

    async def _after_pass(self, user_id, account_id, batch, response, now, zone, writes):
        built = await super()._after_pass(user_id, account_id, batch, response, now, zone, writes)
        return await self._answer("after_pass", now, built, writes)

    async def _send_next(self, user_id, account_id, now, zone, writes):
        built = await super()._send_next(user_id, account_id, now, zone, writes)
        return await self._answer("send", now, built, writes)

    async def _digest(self, user_id, account_id, now, zone):
        return await self._answer("digest", now, await super()._digest(user_id, account_id, now, zone))

    @property
    def stubs(self) -> List[str]:
        return [name for name, _ in self.calls]


class Rig:
    """The world, the service on it, and the neighbours the run reads."""

    def __init__(self, world: ReviewWorld, accounts: Accounts, users: Users,
                 clock: Optional[Callable[[], datetime]] = None, lease: Any = None) -> None:
        self.world = world
        self.accounts = accounts
        self.users = users
        self.agents = Agents()
        self.coordinator = Coordinator()
        zones = OwnerTimezoneResolver(accounts, users)
        texts = FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                  LanguageCode.EN, clock=world.clock)
        answers = FactAnswerService(
            fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
            task_queue=world.queue, session_store=world.session_store, texts=texts, interactive=world.interactive,
            owner_zones=zones, clock=world.clock,
        )
        self.fact_updates = RecordingFactUpdates(world.store.adapter)
        self.refreshes: List[str] = []
        real = world.repo.refresh_biographical_context_cache

        async def refresh(owner_id: str, *args: Any, **kwargs: Any) -> None:
            self.refreshes.append(owner_id)
            await real(owner_id, *args, **kwargs)

        world.repo.refresh_biographical_context_cache = refresh  # type: ignore[method-assign]
        self.service = Recording(
            repo=world.repo, review_store=world.review_store, counters=world.counters, lease=lease or world.lease,
            coordinator=self.coordinator, agent_factory=self.agents, user_repo=users, account_repo=accounts,
            texts=texts, owner_zones=zones, notification_state=world.notification_state,
            channel_bindings=world.channel_bindings, notifications=world.notifier, session_store=world.session_store,
            task_queue=world.queue, answer_service=answers, interactive=world.interactive, alert_sink=world.sink,
            run_timeout_s=600, clock=clock or world.clock,
            fact_updates=self.fact_updates, cabinet_url="https://cabinet.example/cabinet?tab=memory",
        )

    async def run(self, mode: str = "daily") -> Dict[str, Any]:
        return await self.service.run(USER, ACC, mode)  # type: ignore[arg-type]


def owner_profile(account_id: str = ACC) -> UserProfile:
    return UserProfile(user_id=USER, account_id=account_id, config=UserBotConfig(timezone=TZ))


def owned(**roles: str) -> BillingAccount:
    return BillingAccount(account_id=ACC, iam_policy=dict(roles) or {USER: "owner"})


def rig(world: Optional[ReviewWorld] = None, **kwargs: Any) -> Rig:
    return Rig(world or ReviewWorld(), Accounts(owned()), Users(owner_profile()), **kwargs)


async def question(world: ReviewWorld, question_id: str = "q1") -> FactReviewQuestion:
    return (await world.review_store.get_questions([question_id]))[0]


async def state(world: ReviewWorld) -> Optional[FactReviewState]:
    return await world.review_store.get_state(ACC)


async def day_row(world: ReviewWorld):
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0] if rows else None


def lease_doc(world: ReviewWorld) -> Optional[dict]:
    return world.store.db.collection(world.env.memory_write_leases_collection).docs.get(ACC)


def today(world: ReviewWorld) -> date:
    return world.now.date()      # 08:30 in the owner's zone: the same calendar day


def writing(result: _StepResult) -> Callable[[Any], _StepResult]:
    """A scripted step that marks one fact write as it lands, then returns `result`."""

    def step(writes: Any) -> _StepResult:
        writes.wrote()
        return result

    return step


def stale_fact():
    return build_fact("f1", account_id=ACC)


# ------------------------------------------------------------------------------------------------ the owner check


NOT_OWNER_CASES = {
    "not the first owner": (Accounts(owned(**{"a0": "owner", USER: "owner"})), Users(owner_profile())),
    "a profile of another account": (Accounts(owned()), Users(owner_profile("acc-other"))),
    "no account": (Accounts(), Users(owner_profile())),
    "no owner": (Accounts(owned(**{USER: "member"})), Users(owner_profile())),
    "no profile": (Accounts(owned()), Users()),
}


class TestFactReviewServiceRun:
    @pytest.mark.parametrize("mode", ["daily", "next"])
    @pytest.mark.parametrize("case", sorted(NOT_OWNER_CASES))
    async def test_a_user_who_is_not_the_owner_gets_not_owner_and_nothing_else_happens(self, case, mode, caplog):
        world = ReviewWorld()
        accounts, users = NOT_OWNER_CASES[case]
        accounts.asked.clear()
        users.asked.clear()
        r = Rig(world, accounts, users)
        await world.seed_question(QuestionStatus.PENDING, appended_at=None)        # stuck: would be requeued

        with caplog.at_level(logging.WARNING):
            result = await r.run(mode)

        assert result == {"status": "not_owner"}
        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert len(warnings) == 1 and "not the owner" in warnings[0].getMessage()
        assert accounts.asked == [ACC] and users.asked == [USER]      # the check's two reads, nothing further
        assert r.service.calls == [] and r.agents.ensured == [] and r.coordinator.routed == []
        assert lease_doc(world) is None and world.enqueued == [] and await state(world) is None
        assert (await question(world)).status is QuestionStatus.PENDING

    async def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError):
            await rig().run("weekly")

    def test_a_run_timeout_within_the_lease_margin_is_refused(self):
        with pytest.raises(ValueError):
            FactReviewService(**_kwargs_of(rig()), run_timeout_s=60)

    # -------------------------------------------------------------------------------------------- mode="next"

    async def test_next_sends_without_the_lease_setup_or_a_model_call(self):
        world = ReviewWorld()
        r = rig(world)
        await world.lease.acquire(ACC, "consolidation-run", world.now, world.now + timedelta(minutes=30))
        before = dict(lease_doc(world) or {})
        r.service.script["send"] = _StepResult(counts={CounterField.QUESTIONS_SENT: 1})

        result = await r.run("next")

        assert result == {"status": "completed"}                  # not deferred: the lease is not asked for
        assert r.service.stubs == ["send"]
        assert r.agents.ensured == [] and r.coordinator.routed == []
        assert lease_doc(world) == before                          # still the consolidation's
        assert world.enqueued == [] and await state(world) is None   # no deferral, no started/succeeded marks
        assert (await day_row(world)).questions_sent == 1

    # -------------------------------------------------------------------------------------------- the lease

    async def test_a_held_lease_defers_the_run_under_its_named_task_and_a_second_loser_shares_it(self):
        world = ReviewWorld()
        r = rig(world)
        await world.lease.acquire(ACC, "consolidation-run", world.now, world.now + timedelta(minutes=30))
        await world.seed_question(QuestionStatus.PENDING, appended_at=None)

        before = datetime.now(timezone.utc)
        first = await r.run()
        after = datetime.now(timezone.utc)
        second = await r.run()

        assert first == second == {"status": "deferred"}
        # The service asks for a 120 s delay; the queue adapter anchors it at the wall clock of the enqueue.
        scheduled = world.tasks.create_task_requests[0]["task"]["schedule_time"].ToDatetime(tzinfo=timezone.utc)
        assert before + timedelta(seconds=120) <= scheduled + timedelta(microseconds=1)
        assert scheduled <= after + timedelta(seconds=120)
        assert world.enqueued == [("fact_review", {"task_type": "fact_review", "user_id": USER, "account_id": ACC,
                                                   "mode": "daily"})]
        digest = hashlib.sha256(defer_key(ACC, world.now).encode()).hexdigest()[:32]
        assert any(name.endswith(f"/tasks/fact_review-{digest}") for name in world.tasks.names)
        request = world.tasks.create_task_requests[0]["task"]
        assert request["dispatch_deadline"].seconds == 1800 and "schedule_time" in request
        assert defer_key(ACC, world.now) == f"fact-review-defer:{ACC}:{int(world.now.timestamp()) // 120}"
        assert r.service.calls == [] and await state(world) is None     # not started, no housekeeping
        assert (await question(world)).status is QuestionStatus.PENDING

    async def test_the_run_holds_the_lease_a_consolidation_would_take_and_frees_it(self):
        world = ReviewWorld()
        r = rig(world)
        seen: List[bool] = []

        async def try_consolidation(now: datetime) -> list:
            seen.append(await world.lease.acquire(ACC, "consolidation-run", now, now + timedelta(minutes=30)))
            return []

        r.service.script["select"] = try_consolidation

        assert await r.run() == {"status": "completed"}
        assert seen == [False]                                      # a consolidation now loses (and defers)
        assert lease_doc(world) is None                             # released at the end
        assert await world.lease.acquire(ACC, "consolidation-run", world.now, world.now + timedelta(minutes=30))

    async def test_a_lease_release_error_is_logged_and_the_run_stands(self, caplog):
        world = ReviewWorld()

        class FailingRelease:
            async def acquire(self, *args: Any) -> bool:
                return await world.lease.acquire(*args)

            async def release(self, account_id: str, owner: str) -> None:
                raise RuntimeError("lease store down")

        r = rig(world, lease=FailingRelease())
        with caplog.at_level(logging.WARNING):
            assert await r.run() == {"status": "completed"}
        assert any("releasing the memory-write lease failed" in rec.getMessage() for rec in caplog.records)

    # -------------------------------------------------------------------------------------------- steps 9, 13, 15

    async def test_an_empty_selection_makes_no_model_call_and_succeeds(self):
        world = ReviewWorld()
        await world.seed_state(unsuccessful_since=world.now - timedelta(days=3))
        r = rig(world)

        assert await r.run() == {"status": "completed"}

        assert r.service.stubs == ["timeouts", "kept_twice", "select", "send", "digest"]
        assert r.coordinator.routed == [] and r.agents.ensured == [USER]
        stored = await state(world)
        assert stored.started_at == world.now and stored.last_review_at == world.now
        assert stored.unsuccessful_since is None and stored.last_failed_at is None
        assert r.refreshes == []                                   # nothing written, no refresh
        assert await day_row(world) is None                        # nothing to count

    async def test_a_run_on_an_account_with_no_review_state_completes_and_warns_of_each_mark(self, caplog):
        world = ReviewWorld()
        with caplog.at_level(logging.WARNING):
            assert await rig(world).run() == {"status": "completed"}
        assert await state(world) is None                          # only the janitor's enqueue creates the state
        marks = [rec.getMessage() for rec in caplog.records if "has no review state" in rec.getMessage()]
        assert [m.rsplit(" ", 1)[-1] for m in marks] == ["started", "succeeded"]

    async def test_an_agent_failure_is_200_shaped_counted_and_the_send_and_digest_still_run(self):
        world = ReviewWorld()
        await world.seed_state()                                    # as the janitor's enqueue leaves it
        r = rig(world)
        r.service.script.update(
            kept_twice=writing(_StepResult(counts={CounterField.QUEUED: 1})),
            select=lambda now: [stale_fact()],
            route=AgentResponse.failure("run-1", "consolidation_agent_u1", "provider down"),
        )

        assert await r.run() == {"status": "failed"}

        assert r.service.stubs == ["timeouts", "kept_twice", "select", "route", "send", "digest"]   # no after-pass
        row = await day_row(world)
        assert (row.review_failed, row.queued) == (1, 1)
        stored = await state(world)
        assert stored.last_failed_at == world.now and stored.unsuccessful_since == world.now
        assert stored.last_review_at is None
        assert r.refreshes == [ACC]                                 # the service's write is refreshed, once
        assert lease_doc(world) is None

    async def test_writes_of_several_steps_refresh_the_cache_once(self):
        world = ReviewWorld()
        r = rig(world)
        r.service.script.update(
            timeouts=writing(_Timeouts()), kept_twice=writing(_StepResult()),
            select=lambda now: [stale_fact()],
            route=AgentResponse.success("run-1", "consolidation_agent_u1", {}),
            after_pass=writing(_StepResult(counts={CounterField.REVIEWED: 3})),
        )

        assert await r.run() == {"status": "completed"}

        assert r.service.stubs == ["timeouts", "kept_twice", "select", "route", "after_pass", "send", "digest"]
        assert r.refreshes == [ACC]
        assert (await day_row(world)).reviewed == 3

    async def test_a_store_error_after_a_write_propagates_and_the_cache_is_still_refreshed(self):
        world = ReviewWorld()
        await world.seed_state()
        r = rig(world)
        r.service.script.update(kept_twice=writing(_StepResult()))
        original = r.service._send_next

        async def failing_send(user_id, account_id, now, zone, writes):
            await original(user_id, account_id, now, zone, writes)
            raise RuntimeError("store unavailable")

        r.service._send_next = failing_send  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="store unavailable"):
            await r.run()

        assert r.refreshes == [ACC]
        assert lease_doc(world) is None                             # released in finally
        assert (await state(world)).last_review_at is None

    @pytest.mark.parametrize("step", ["timeouts", "kept_twice", "after_pass", "send"])
    async def test_a_step_that_writes_a_fact_and_then_raises_still_gets_one_refresh(self, step):
        world = ReviewWorld()
        await world.seed_state()
        r = rig(world)

        def writes_then_fails(writes: Any) -> None:
            writes.wrote()
            raise RuntimeError("queue unavailable")

        r.service.script.update(select=lambda now: [stale_fact()],
                                route=AgentResponse.success("run-1", "consolidation_agent_u1", {}))
        r.service.script[step] = writes_then_fails

        with pytest.raises(RuntimeError, match="queue unavailable"):
            await r.run()

        assert r.service.stubs[-1] == step                          # nothing ran after the failing step
        assert r.refreshes == [ACC]
        assert lease_doc(world) is None
        assert (await state(world)).last_review_at is None

    async def test_a_next_send_that_writes_a_fact_and_then_raises_still_gets_one_refresh(self):
        world = ReviewWorld()
        r = rig(world)

        def writes_then_fails(writes: Any) -> None:
            writes.wrote()
            raise RuntimeError("queue unavailable")

        r.service.script["send"] = writes_then_fails

        with pytest.raises(RuntimeError, match="queue unavailable"):
            await r.run("next")

        assert r.refreshes == [ACC]

    # -------------------------------------------------------------------------------------------- housekeeping

    async def test_a_stuck_question_without_a_ref_is_retracted_and_goes_back_to_the_front_with_its_key(self):
        world = ReviewWorld()
        seeded = await world.seed_question(QuestionStatus.PENDING, appended_at=world.now - timedelta(days=1))
        await world.seed_state(current_question_id="q1", sent_on=today(world) - timedelta(days=1), sent=1)

        assert await rig(world).run() == {"status": "completed"}

        back = await question(world)
        assert back.status is QuestionStatus.QUEUED and back.queue_key == seeded.queue_key
        assert back.session_id is None and back.appended_at is None
        after = await state(world)
        assert after.current_question_id is None
        assert after.delivery_failed_on is None                     # housekeeping is no failed delivery of today
        history = await world.session_store.messages(SESSION)
        assert [(m.role, m.parts[0].text) for m in history] == [
            ("user", not_delivered_frame("q1")), ("model", NOT_DELIVERED_REPLY)]
        assert history[1].parts[0].full_text == NOT_DELIVERED_REPLY

    async def test_a_stuck_question_whose_frame_was_never_appended_is_requeued_with_no_pair(self):
        world = ReviewWorld()
        await world.seed_question(QuestionStatus.PENDING, appended_at=None)        # not current: its send is over

        assert await rig(world).run() == {"status": "completed"}

        assert (await question(world)).status is QuestionStatus.QUEUED
        assert await world.session_store.messages(SESSION) == []

    async def test_a_stuck_question_with_a_ref_becomes_asked_at_its_append_with_no_pair(self):
        world = ReviewWorld()
        appended = world.now - timedelta(hours=1)                   # today in the owner's zone, so it stays asked
        await world.seed_question(QuestionStatus.PENDING, posted=True, appended_at=appended)

        assert await rig(world).run() == {"status": "completed"}

        asked = await question(world)
        assert asked.status is QuestionStatus.ASKED and asked.asked_at == appended
        assert await world.session_store.messages(SESSION) == []

    async def test_yesterdays_stuck_current_question_with_a_ref_is_completed_then_unanswered(self):
        world = ReviewWorld()
        appended = world.now - timedelta(days=1)
        await world.seed_question(QuestionStatus.PENDING, posted=True, appended_at=appended)
        await world.seed_state(current_question_id="q1", sent_on=today(world) - timedelta(days=1), sent=1)

        await rig(world).run()

        moved = await question(world)
        assert moved.status is QuestionStatus.UNANSWERED and moved.asked_at == appended
        assert (await state(world)).current_question_id is None
        assert await world.session_store.messages(SESSION) == []

    async def test_a_question_claimed_today_by_a_next_send_is_left_alone(self):
        world = ReviewWorld()
        await world.seed_question(QuestionStatus.PENDING, appended_at=world.now - timedelta(minutes=1))
        await world.seed_state(current_question_id="q1", sent_on=today(world), sent=1)

        assert await rig(world).run() == {"status": "completed"}

        left = await question(world)
        assert left.status is QuestionStatus.PENDING and left.session_id == SESSION
        assert (await state(world)).current_question_id == "q1"
        assert await world.session_store.messages(SESSION) == []

    async def test_the_current_question_from_an_earlier_day_becomes_unanswered(self):
        world = ReviewWorld()
        await world.seed_question(QuestionStatus.ASKED, asked_at=world.now - timedelta(days=1))
        await world.seed_question(QuestionStatus.ASKED, question_id="q2", fact_id="f2",
                                  asked_at=world.now - timedelta(hours=1),
                                  message_ref=MessageRef(platform="slack", channel_id="D1", message_id="m2"))
        await world.seed_state(current_question_id="q1", sent_on=today(world) - timedelta(days=1), sent=1)

        await rig(world).run()

        assert (await question(world)).status is QuestionStatus.UNANSWERED
        assert (await question(world, "q2")).status is QuestionStatus.ASKED         # asked today: still out
        assert (await state(world)).current_question_id is None

    async def test_an_asked_question_from_an_earlier_day_that_is_not_current_becomes_unanswered_pointer_untouched(self):
        world = ReviewWorld()
        await world.seed_question(QuestionStatus.ASKED, asked_at=world.now - timedelta(days=1))
        await world.seed_question(QuestionStatus.ASKED, question_id="q2", fact_id="f2",
                                  asked_at=world.now - timedelta(hours=1),
                                  message_ref=MessageRef(platform="slack", channel_id="D1", message_id="m2"))
        await world.seed_state(current_question_id="q2", sent_on=today(world), sent=1)

        await rig(world).run()

        assert (await question(world)).status is QuestionStatus.UNANSWERED
        assert (await question(world, "q2")).status is QuestionStatus.ASKED
        assert (await state(world)).current_question_id == "q2"     # the pointer names the other one: left as it was

    async def test_a_failed_retraction_append_raises_and_leaves_the_question_pending(self):
        world = ReviewWorld()
        await world.seed_question(QuestionStatus.PENDING, appended_at=world.now - timedelta(days=1))
        world.session_store.fail = True

        with pytest.raises(RuntimeError):
            await rig(world).run()

        assert (await question(world)).status is QuestionStatus.PENDING
        assert lease_doc(world) is None

    # -------------------------------------------------------------------------------------------- one now

    async def test_every_step_gets_the_one_now_read_at_the_start(self):
        world = ReviewWorld()
        await world.seed_state()
        ticks: List[datetime] = []

        def ticking() -> datetime:
            ticks.append(world.now + timedelta(seconds=len(ticks)))
            return ticks[-1]

        r = rig(world, clock=ticking)
        r.service.script.update(select=lambda now: [stale_fact()],
                                route=AgentResponse.success("run-1", "consolidation_agent_u1", {}))

        await r.run()

        # One read for the run; the only other is the lease budget at routing (T8a review F2): ticks[1] reaches the
        # agent's timeout alone, (ticks[0] + 600 s + 60 s margin) - 60 s margin - (ticks[0] + 1 s).
        assert len(ticks) == 2
        assert r.coordinator.routed[0].timeout_ms == 599_000
        assert {now for _, now in r.service.calls} == {ticks[0]}
        assert r.service.stubs == ["timeouts", "kept_twice", "select", "route", "after_pass", "send", "digest"]
        stored = await state(world)
        assert stored.started_at == stored.last_review_at == ticks[0]


def _kwargs_of(r: Rig) -> Dict[str, Any]:
    s = r.service
    return dict(
        repo=s._repo, review_store=s._review_store, counters=s._counters, lease=s._lease, coordinator=s._coordinator,
        agent_factory=s._agent_factory, user_repo=s._user_repo, account_repo=s._account_repo, texts=s._texts,
        owner_zones=s._owner_zones, notification_state=s._notification_state, channel_bindings=s._channel_bindings,
        notifications=s._notifications, session_store=s._session_store, task_queue=s._task_queue,
        answer_service=s._answer_service, interactive=s._interactive, alert_sink=s._alert_sink,
        fact_updates=s._fact_updates, cabinet_url=s._cabinet_url,
    )
