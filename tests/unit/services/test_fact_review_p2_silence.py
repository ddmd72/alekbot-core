"""Facts P2 · card B: a silent `history` question is written as history (C9). The plain service on the review world with the
scripted agent of the T8b tests: the real review store, repository, `FactAnswerService` and the review world's real fact
write, behind the recording `FactUpdatePort`. Assertions are on the stored facts, questions, counters and state."""
import logging
from datetime import timedelta
from typing import Any, List


from src.domain.entities import ExpiryKind
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_update_outcome import UpdateRefusal
from src.domain.pass_actor import PassActor
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_review_selection import USER, Rig, day_row, stored_question

TALK = "Alex gives a talk at the Hamburg conference"
TEXTS = {
    "happened": "Gave a talk at the Hamburg conference in spring 2026",
    "not_happened": "The Hamburg conference talk did not take place",
    "unknown": "Planned a talk at the Hamburg conference; unknown whether it took place",
}


async def dated_fact(world: ReviewWorld, text: str = TALK) -> str:
    """A current fact of expiry kind `date`, its day eleven days gone."""
    return await world.seed(text, expiry_kind=ExpiryKind.DATE, expires_at=NOW - timedelta(days=11),
                            reported_date=NOW - timedelta(days=60))


async def silent(world: ReviewWorld, question_id: str, fact_id: str, **over: Any):
    """A `happened` question the review asked, fifteen days unanswered."""
    over.setdefault("question", "happened")
    over.setdefault("texts", TEXTS)
    over.setdefault("fallback", "history")
    over.setdefault("asked_at", NOW - timedelta(days=15))
    return await world.seed_question(
        QuestionStatus.UNANSWERED, question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact_id,
        quoted_text=world.docs[fact_id]["text"], **over)


def written_versions(world: ReviewWorld, lineage_of: str) -> List[dict]:
    lineage = world.docs[lineage_of]["lineage_id"]
    return sorted((d for d in world.docs.values() if d.get("lineage_id") == lineage), key=lambda d: d["version"])


class TestSilenceWritesHistory:
    async def test_a_silent_happened_question_writes_its_unknown_text_as_history_under_the_timeout_actor(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "qt", fact)
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        question = await stored_question(world, "qt")
        assert question.status is QuestionStatus.CLOSED and question.closed_by is ClosedBy.TIMEOUT
        old, new = written_versions(world, fact)
        assert new["text"] == TEXTS["unknown"] and new["state"] == "current" and new["expiry_kind"] == "none"
        assert new["context_priority"] == "archival"
        assert old["superseded_by"] == new["id"]
        assert new["lifecycle_events"][-1]["actor"] == "timeout"
        assert new["lifecycle_events"][-1]["reason"] == "rewritten as history: no answer to q=qt"
        [call] = r.service._fact_updates.calls
        assert call.context.actor is PassActor.TIMEOUT and call.update.as_history is True
        row = await day_row(world)
        assert (row.timed_out, row.to_history) == (1, 1)
        assert (await world.review_store.get_state(ACC)).undigested_since == NOW

    async def test_a_kept_twice_date_question_writes_the_code_built_text_dated_in_the_runs_month(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "qk", fact, origin="kept_twice", texts={})
        await world.seed_state(account_id=ACC)

        await Rig(world).run()

        _, new = written_versions(world, fact)
        assert new["text"] == f"Planned; whether it took place is unknown (no answer by Oct 2026): {TALK}"
        assert (await stored_question(world, "qk")).closed_by is ClosedBy.TIMEOUT


class TestAFailingHistoryWrite:
    async def test_a_raising_write_counts_one_attempt_keeps_the_question_open_and_the_run_goes_on(self, caplog):
        world = ReviewWorld()
        fact = await dated_fact(world)
        other = await dated_fact(world, "Alex has a pottery class on Saturday")
        await silent(world, "q1", fact)
        await silent(world, "q2", other, asked_at=NOW - timedelta(days=14, hours=1))
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        r.service._fact_updates.script(RuntimeError("store down"))

        with caplog.at_level(logging.WARNING):
            assert await r.run() == {"status": "completed"}

        failed = await stored_question(world, "q1")
        assert failed.is_open and failed.fallback_attempts == 1 and failed.closed_by is None
        assert world.docs[fact]["text"] == TALK                                    # nothing written for it
        assert (await stored_question(world, "q2")).closed_by is ClosedBy.TIMEOUT   # the next one still ran
        row = await day_row(world)
        assert (row.fallbacks_not_applied, row.timed_out, row.to_history) == (1, 1, 1)
        assert any("q1" in rec.getMessage() and "store down" in rec.getMessage() for rec in caplog.records)
        assert not any(TALK in rec.getMessage() for rec in caplog.records)         # a fact's text is never logged
        assert [q.question_id for q in await world.review_store.questions_for_account(ACC)] == ["q1"]   # no second question

    async def test_a_typed_refusal_at_the_second_attempt_closes_the_question_resolved_elsewhere(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "q1", fact, fallback_attempts=1)
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        r.service._fact_updates.refuse_next(fact, UpdateRefusal.INVALID)

        await r.run()

        question = await stored_question(world, "q1")
        assert question.closed_by is ClosedBy.RESOLVED_ELSEWHERE
        assert (await day_row(world)).fallbacks_not_applied == 1

    async def test_a_typed_refusal_at_the_first_attempt_leaves_the_question_open(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "q1", fact)
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        r.service._fact_updates.refuse_next(fact, UpdateRefusal.INVALID)

        await r.run()

        question = await stored_question(world, "q1")
        assert question.is_open and question.fallback_attempts == 1

    async def test_an_exception_at_the_third_attempt_closes_the_question_resolved_elsewhere(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "q1", fact, fallback_attempts=2)
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        r.service._fact_updates.script(RuntimeError("store down"))

        await r.run()

        assert (await stored_question(world, "q1")).closed_by is ClosedBy.RESOLVED_ELSEWHERE

    async def test_an_exception_at_the_second_attempt_stays_open(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "q1", fact, fallback_attempts=1)
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        r.service._fact_updates.script(RuntimeError("store down"))

        await r.run()

        question = await stored_question(world, "q1")
        assert question.is_open and question.fallback_attempts == 2

    async def test_a_conflict_leaves_the_question_for_the_next_run_and_counts_nothing(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "q1", fact)
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        r.service._fact_updates.refuse_next(fact, UpdateRefusal.CONFLICT)

        await r.run()

        question = await stored_question(world, "q1")
        assert question.is_open and question.fallback_attempts == 0
        assert await day_row(world) is None or (await day_row(world)).fallbacks_not_applied == 0
        assert (await world.review_store.get_state(ACC)).undigested_since is None

    async def test_an_attempt_the_store_cannot_count_is_a_warning_and_the_run_goes_on(self, caplog, monkeypatch):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "q1", fact)
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        r.service._fact_updates.script(RuntimeError("store down"))

        async def changed_meanwhile(question_id: str) -> bool:                       # the question closed under us
            return False

        monkeypatch.setattr(world.review_store, "mark_fallback_failed", changed_meanwhile)

        with caplog.at_level(logging.WARNING):
            assert await r.run() == {"status": "completed"}

        assert any("was not counted" in rec.getMessage() for rec in caplog.records)
