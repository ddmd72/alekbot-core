"""Facts P2 · final review: a typed write names the exact document it decided on (`expected_id`), and a silence whose own
history write landed but whose close did not is closed as the timeout it was. The real adapter on the review world; a
fact is moved (rewritten by someone else) just before the write by the recording port's `before` hook."""
from src.domain.entities import ExpiryKind
from src.domain.fact_expiry import EXPIRY_NONE
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_batch import ReviewBatch
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_outcome import UpdateRefusal
from src.domain.fact_write_context import FactWriteContext
from src.domain.pass_actor import PassActor
from src.infrastructure.fact_review_scope import FactReviewScope
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_fact_updates import RecordingFactUpdates
from tests.unit.services.test_fact_answer_service import USER as ANSWERER, service
from tests.unit.services.test_fact_review_p2_silence import TEXTS, dated_fact, silent, written_versions
from tests.unit.services.test_fact_review_selection import Rig, stored_question

MOVED_TEXT = "Alex gives a talk at the Hamburg conference in the main hall"
OWNER = FactWriteContext.for_pass(PassActor.OWNER, "UTC", None)


async def move(world: ReviewWorld, fact_id: str) -> str:
    """Someone else rewrites the fact: a new version of its lineage."""
    outcome = await world.store.adapter.apply_fact_update(
        fact_id, ParsedFactUpdate(content=MOVED_TEXT, expiry=EXPIRY_NONE), ACC, context=OWNER, reason="rewritten q=other: moved by someone else",
        evidence_at=NOW,
    )
    assert outcome.written, outcome
    return outcome.written_id


def mover(world: ReviewWorld, fact_id: str) -> RecordingFactUpdates:
    port = RecordingFactUpdates(world.store.adapter)

    async def before(_: str) -> None:
        await move(world, fact_id)

    port.before = before
    return port


async def test_the_adapter_refuses_moved_when_the_first_resolution_is_not_the_expected_document():
    world = ReviewWorld()
    fact = await dated_fact(world)
    new = await move(world, fact)

    outcome = await world.store.adapter.apply_fact_update(
        fact, ParsedFactUpdate(content="A text nobody asked for"), ACC,
        context=FactWriteContext.for_pass(PassActor.STAGE1, "UTC", None), expected_id=fact,
    )

    assert outcome.refusal is UpdateRefusal.MOVED
    assert world.docs[new]["text"] == MOVED_TEXT and len(written_versions(world, fact)) == 2


async def test_the_adapter_follows_the_chain_when_no_expected_id_is_given():
    world = ReviewWorld()
    fact = await dated_fact(world)
    await move(world, fact)

    outcome = await world.store.adapter.apply_fact_update(
        fact, ParsedFactUpdate(content="Alex gives a talk in Hamburg", expiry=EXPIRY_NONE), ACC, context=FactWriteContext.for_pass(PassActor.STAGE1, "UTC", None),
    )

    assert outcome.written


async def test_the_history_fallback_moved_before_its_write_writes_nothing_and_counts_the_attempt():
    world = ReviewWorld()
    fact = await dated_fact(world)
    await silent(world, "q1", fact)
    await world.seed_state(account_id=ACC)
    r = Rig(world)
    r.service._fact_updates = mover(world, fact)

    await r.run()

    [call] = r.service._fact_updates.calls
    assert call.expected_id == fact
    assert not any(d["text"] == TEXTS["unknown"] for d in world.docs.values())
    question = await stored_question(world, "q1")
    assert question.is_open and question.fallback_attempts == 1


async def test_an_owner_answer_whose_fact_moved_before_the_write_writes_nothing():
    world = ReviewWorld()
    fact = await dated_fact(world)
    await world.seed_question(
        QuestionStatus.QUEUED, question_id="q1", account_id=ACC, user_id=ANSWERER, fact_id=fact,
        quoted_text=world.docs[fact]["text"], question="happened", texts=TEXTS, fallback="history")
    port = mover(world, fact)

    result = await service(world, fact_updates=port).apply(ACC, ANSWERER, "q1", AnswerKind.HAPPENED, "cabinet", world.now)

    assert result.value == "not_applied"
    assert port.calls[0].expected_id == fact
    assert not any(d["text"] == TEXTS["happened"] for d in world.docs.values())
    assert world.question_docs["q1"]["status"] == "queued"


async def test_the_review_scope_names_the_document_it_expects_and_a_moved_fact_is_refused():
    world = ReviewWorld()
    fact = await world.seed_stale("Alex gives a talk at the Hamburg conference", expiry_kind=ExpiryKind.DATE)
    batch_fact = await world.repo.get_fact_by_id(fact)
    port = mover(world, fact)
    scope = FactReviewScope(ReviewBatch((batch_fact,), ()), ACC, "Europe/Lisbon", world.now, world.repo, port)

    res = await scope.update_fact({
        "fact_id": fact, "basis": {"kind": "date_passed"}, "as_history": True,
        "updates": {"state": "current", "content": TEXTS["unknown"], "expiry": {"kind": "none"}},
    })

    assert port.calls[0].expected_id == fact
    assert res["status"] != "updated" and scope.decisions() == []
    assert not any(d["text"] == TEXTS["unknown"] for d in world.docs.values())


class TestASilenceWhoseCloseWasLost:
    async def test_the_question_closes_as_a_timeout_and_the_account_is_marked_undigested(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        question = await silent(world, "q1", fact)
        await world.seed_state(account_id=ACC)
        # the fallback's write landed (the run died before the close): the fact is now the history version
        await world.store.adapter.apply_fact_update(
            fact,
            ParsedFactUpdate(content=TEXTS["unknown"], expiry=EXPIRY_NONE, as_history=True),
            ACC, context=FactWriteContext.for_pass(PassActor.TIMEOUT, "Europe/Lisbon", None), evidence_at=NOW,
            reason=f"rewritten as history: no answer to q={question.question_id}",
        )

        await Rig(world).run()

        closed = await stored_question(world, "q1")
        assert closed.closed_by is ClosedBy.TIMEOUT
        assert (await world.review_store.get_state(ACC)).undigested_since == NOW

    async def test_a_fact_rewritten_by_someone_else_is_still_resolved_elsewhere(self):
        world = ReviewWorld()
        fact = await dated_fact(world)
        await silent(world, "q1", fact)
        await world.seed_state(account_id=ACC)
        await move(world, fact)

        await Rig(world).run()

        assert (await stored_question(world, "q1")).closed_by is ClosedBy.RESOLVED_ELSEWHERE
