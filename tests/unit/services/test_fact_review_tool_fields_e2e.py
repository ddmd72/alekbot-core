"""P14 card C: the review pass inside the review service, every field of the review's `update_fact` and `ask_owner` to
where the service stores it.

The real `ConsolidationAgent` runs inside the real `FactReviewService.run` (the selection test's `Rig` on the review
world, its coordinator replaced by a real `AgentCoordinator` holding the agent), over a scripted `LLMPort` (card A's
`RoutedLLM`): the review pass answers its scripted tool turns from a queue, then the report. The agent's `_call_llm`,
its loop, the `FactReviewScope`, the typed write path, the service's after-pass (questions, folds, invoices) and the
stores run for real. Assertions read the stored fact documents, their trail reasons and the stored questions; a refusal
is read from the tool results the model received, and fixed in the script, never in the code.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig
from src.domain.consolidation import consolidation_agent_id
from src.domain.entities import ExpiryKind
from src.domain.fact_write_context import as_utc
from src.domain.llm import LLMRequest, LLMResponse
from src.infrastructure.agent_coordinator import AgentCoordinator
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.scripted_llm import RoutedLLM, execution_context, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder
from tests.support.tool_fields import unused
from tests.unit.services.test_fact_review_selection import USER, Rig, asked, current, evidence, open_questions, stale

REVIEW = "SYSTEM MAINTENANCE — STALE FACT REVIEW"
REPORT = LLMResponse(text='{"operations": []}')
# A date the write path does not refuse as born due (it reads the real clock): the next year's Oct 20.
NEXT_OCT_20 = f"{datetime.now(timezone.utc).year + 1}-10-20"

# ------------------------------------------------------------------------------------------- the facts

FITLIFE = "Alex trains at the FitLife gym"
IRON_TEMPLE = "Alex trains at the Iron Temple gym"
REWRITTEN = "Alex trains at the Iron Temple gym, after leaving FitLife"
PHONE_BILL = "Alex's monthly phone bill of 42.50 € is due Oct 20, 2026"
PHONE_HISTORY = "Alex's monthly phone bill of 42.50 € was due Oct 20, 2026, payment not confirmed"
PHONE_PAID = "Alex paid the monthly phone bill of 42.50 € due Oct 20, 2026"
PHONE_NOT_PAID = "Alex has not paid the monthly phone bill of 42.50 € due Oct 20, 2026"
POTTERY = "Alex takes pottery classes"
POTTERY_REPLY = "I stopped the pottery classes this month"
POTTERY_HISTORY = "Alex took pottery classes until Oct 2026"
CHESS = "Alex plays chess on Sundays"
TENNIS = "Alex plays tennis on Sundays"
STUDIO = "Alex rents a pottery studio"
STUDIO_RENEWED = "Alex renewed the rent of the pottery studio"
TALK = "Alex gives a talk at the Hamburg conference on Oct 15, 2026"
TRAIN = "Alex booked a train for the Hamburg conference trip"
HOTEL = "Alex booked a hotel for the Hamburg conference trip"
GYM = "Alex's gym membership is valid until Oct 20, 2026"
FINE = "Alex has an open parking fine of 80 € due Nov 30, 2026"
TEA = "The user drank green tea until Oct 2026"

TALK_TEXTS = {"happened": "Alex gave a talk at the Hamburg conference on Oct 15, 2026",
              "not_happened": "Alex planned a talk at the Hamburg conference on Oct 15, 2026 and did not give it",
              "unknown": "Alex planned a talk at the Hamburg conference on Oct 15, 2026; unknown whether it took place"}
GYM_TEXTS = {"renewed": "Alex's gym membership was renewed to Oct 20, 2027",
             "not_renewed": "Alex's gym membership ended on Oct 20, 2026",
             "not_needed": "Alex's gym membership ended on Oct 20, 2026 and is no longer needed",
             "unknown": "Alex's gym membership was valid until Oct 20, 2026; unknown whether it was renewed"}
FINE_TEXTS = {"paid": "Alex paid the parking fine of 80 €",
              "not_paid": "Alex has not paid the parking fine of 80 €",
              "unknown": "Alex had a parking fine of 80 € due Nov 30, 2026; unknown whether it was paid"}


# ------------------------------------------------------------------------------------------- the scripted calls
# The fact ids are known only once seeded, so each call is a builder; the field shapes below are the module constants
# the last test reads (`unused` reads keys, not values).

def REVIEW_REWRITE(fact_id: str, evidence_id: str) -> Dict[str, Any]:
    return {"fact_id": fact_id,
            "updates": {"state": "current", "content": REWRITTEN, "tags": ["gym"], "metadata": {"gym": "Iron Temple"},
                        "temporal_class": "dynamic", "expiry": {"kind": "horizon", "days": 30}},
            "basis": {"kind": "evidence", "evidence_fact_id": evidence_id}}


def REVIEW_HISTORY(fact_id: str) -> Dict[str, Any]:
    return {"fact_id": fact_id,
            "updates": {"state": "current", "content": PHONE_HISTORY, "expiry": {"kind": "none"}},
            "as_history": True, "basis": {"kind": "date_passed", "date": "2026-10-20"},
            "payment": {"paid": PHONE_PAID, "not_paid": PHONE_NOT_PAID}}


def REVIEW_ARCHIVE(fact_id: str, question_id: str) -> Dict[str, Any]:
    return {"fact_id": fact_id, "updates": {"state": "archived"},
            "basis": {"kind": "reply", "question_id": question_id, "quote": "stopped the pottery classes"},
            "history": POTTERY_HISTORY}


def REVIEW_SUPERSEDE(fact_id: str, successor_id: str) -> Dict[str, Any]:
    return {"fact_id": fact_id, "updates": {"state": "superseded", "superseded_by": successor_id}}


def REVIEW_REVIVE(fact_id: str, evidence_id: str) -> Dict[str, Any]:
    return {"fact_id": fact_id, "updates": {"state": "current", "expiry": {"kind": "date", "date": NEXT_OCT_20}},
            "basis": {"kind": "evidence", "evidence_fact_id": evidence_id}}


def ASK_HAPPENED(fact_id: str, *folds: str) -> Dict[str, Any]:
    return {"fact_id": fact_id, "question": "happened", "texts": dict(TALK_TEXTS), "fallback": "history",
            "folds": list(folds)}


def ASK_RENEWED(fact_id: str) -> Dict[str, Any]:
    return {"fact_id": fact_id, "question": "renewed", "texts": dict(GYM_TEXTS), "fallback": "history"}


def ASK_PAID(fact_id: str) -> Dict[str, Any]:
    return {"fact_id": fact_id, "question": "paid", "texts": dict(FINE_TEXTS), "fallback": "history"}


def ASK_NOISE(fact_id: str) -> Dict[str, Any]:
    return {"fact_id": fact_id, "question": "still_true", "fallback": "archive", "noise": True}


# ------------------------------------------------------------------------------------------- the rig


class ReviewPass:
    """The scripted model of one review run: the review pass drains `turns`, then reports."""

    def __init__(self) -> None:
        self.turns: List[LLMResponse] = []
        self.llm = RoutedLLM(self._answer, pending=lambda: len(self.turns))

    def _answer(self, request: LLMRequest) -> Any:
        first = request.messages[0].parts[0].text or ""
        if REVIEW in first:
            return self.turns.pop(0) if self.turns else REPORT
        return None  # an unexpected pass: the fake raises ScriptExhausted


def rig(world: ReviewWorld, script: ReviewPass) -> Rig:
    """The selection test's rig with a real coordinator holding a real consolidation agent over the scripted model."""
    r = Rig(world)
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id=consolidation_agent_id(USER), agent_type="consolidation"),
        execution_context=execution_context(script.llm, "consolidation"),
        repository=world.repo, embedding_service=world.store.embedder, fact_write_service=world.store.write,
        fact_management_port=world.store.adapter, prompt_builder=StubPromptBuilder(prompt="REVIEW SYSTEM PROMPT"),
        alert_sink=world.sink, lifecycle_counters=world.counters, fact_updates=world.store.adapter,
    )
    coordinator = AgentCoordinator()
    coordinator.register_agent(agent)
    r.service._coordinator = coordinator
    return r


def results(llm: RoutedLLM, name: str) -> List[Any]:
    return [r for n, r in tool_results(llm) if n == name]


def by_text(world: ReviewWorld, text: str) -> Dict[str, Any]:
    (doc,) = [d for d in world.docs.values() if d["text"] == text]
    return doc


def id_of(world: ReviewWorld, text: str) -> str:
    (fact_id,) = [k for k, d in world.docs.items() if d["text"] == text]
    return fact_id


def script_spent(llm: RoutedLLM) -> None:
    assert llm.unanswered == 0 and llm.pending() == 0


# ------------------------------------------------------------------------------------------- the tests


async def test_a_rewrite_with_every_update_field_writes_a_version_and_its_reason():
    world = ReviewWorld()
    fact = await stale(world, FITLIFE, NOW - timedelta(days=3))
    proof = await evidence(world, IRON_TEMPLE)
    script = ReviewPass()
    script.turns.append(tools(("update_fact", REVIEW_REWRITE(fact, proof))))

    before = datetime.now(timezone.utc)
    assert await rig(world, script).run() == {"status": "completed"}
    after = datetime.now(timezone.utc)

    (result,) = results(script.llm, "update_fact")
    assert result["status"] == "updated", result
    version = world.docs[result["fact_id"]]
    assert result["fact_id"] != fact and world.docs[fact]["superseded_by"] == result["fact_id"]
    assert version["text"] == REWRITTEN and version["state"] == "current"
    assert version["tags"] == ["gym"]
    assert version["metadata"] == {"gym": "Iron Temple"}
    assert version["temporal_class"] == "dynamic"
    assert version["expiry_kind"] == "horizon"
    # A review horizon counts from the later of the reported date and now, stored as whole days from the reported date
    # (`horizon_from_now`); the write path reads the real clock, not the world's, so only its window is asserted.
    expires_at = as_utc(version["expires_at"])
    assert expires_at == as_utc(version["reported_date"]) + timedelta(days=version["ttl_days"])
    assert before + timedelta(days=30) <= expires_at <= after + timedelta(days=31)
    assert version["lifecycle_events"][-1]["reason"] == f"rewritten on {proof[:8]}"
    script_spent(script.llm)


async def test_a_history_rewrite_of_a_routine_invoice_queues_its_digest_question():
    world = ReviewWorld()
    bill = await stale(world, PHONE_BILL, NOW - timedelta(days=2))
    script = ReviewPass()
    script.turns.append(tools(("update_fact", REVIEW_HISTORY(bill))))

    assert await rig(world, script).run() == {"status": "completed"}

    (result,) = results(script.llm, "update_fact")
    assert result["status"] == "updated", result
    history = world.docs[result["fact_id"]]
    assert history["text"] == PHONE_HISTORY and history["state"] == "current"
    assert history["context_priority"] == "archival"
    assert history["expiry_kind"] == "none"
    assert history["lifecycle_events"][-1]["reason"] == "rewritten as history: date passed 2026-10-20"
    (digest,) = [q for q in await open_questions(world) if q.delivery == "digest"]
    assert digest.fact_id == result["fact_id"] and digest.question == "paid"
    assert digest.texts == {"paid": PHONE_PAID, "not_paid": PHONE_NOT_PAID}
    script_spent(script.llm)


async def test_an_archive_on_the_owners_reply_writes_the_history_fact():
    """The pin of `basis.quote`: a verification input (it must be words of the reply), never stored."""
    world = ReviewWorld()
    fact = await current(world, POTTERY)
    await asked(world, "q1", fact, reply_text=POTTERY_REPLY, reply_at=NOW - timedelta(hours=1))
    script = ReviewPass()
    script.turns.append(tools(("update_fact", REVIEW_ARCHIVE(fact, "q1"))))

    assert await rig(world, script).run() == {"status": "completed"}

    (result,) = results(script.llm, "update_fact")
    assert result["status"] == "updated", result
    assert world.docs[fact]["state"] == "archived"
    assert world.docs[fact]["lifecycle_events"][-1]["reason"] == "archived on reply q=q1"
    history = by_text(world, POTTERY_HISTORY)
    assert history["state"] == "current"
    script_spent(script.llm)


async def test_a_supersede_and_a_revive_with_a_date_expiry():
    world = ReviewWorld()
    chess = await stale(world, CHESS, NOW - timedelta(days=4))
    studio = await stale(world, STUDIO, NOW - timedelta(days=3))
    tennis = await evidence(world, TENNIS)
    renewed = await evidence(world, STUDIO_RENEWED)
    script = ReviewPass()
    script.turns.append(tools(("update_fact", REVIEW_SUPERSEDE(chess, tennis)),
                              ("update_fact", REVIEW_REVIVE(studio, renewed))))

    assert await rig(world, script).run() == {"status": "completed"}

    supersede, revive = results(script.llm, "update_fact")
    assert supersede["status"] == "updated" and revive["status"] == "updated", (supersede, revive)
    assert world.docs[chess]["state"] == "superseded" and world.docs[chess]["superseded_by"] == tennis
    assert world.docs[studio]["state"] == "current"
    assert world.docs[studio]["expiry_kind"] == "date"
    year = int(NEXT_OCT_20[:4])
    assert as_utc(world.docs[studio]["expires_at"]) == datetime(year, 10, 20, 23, tzinfo=timezone.utc)  # end, Lisbon
    script_spent(script.llm)


async def test_asks_of_every_kind_store_their_questions_texts_fallbacks_and_folds():
    world = ReviewWorld()
    talk = await stale(world, TALK, NOW - timedelta(days=9))
    train = await stale(world, TRAIN, NOW - timedelta(days=8))
    hotel = await stale(world, HOTEL, NOW - timedelta(days=7))
    gym = await world.seed_stale(GYM, expiry_kind=ExpiryKind.DATE, reported_date=NOW - timedelta(days=60),
                                 expires_at=datetime(2026, 10, 20, 23, tzinfo=timezone.utc))
    fine = await stale(world, FINE, NOW - timedelta(days=5))
    tea = await stale(world, TEA, NOW - timedelta(days=4))
    script = ReviewPass()
    script.turns.append(tools(("ask_owner", ASK_HAPPENED(talk, train, hotel)), ("ask_owner", ASK_RENEWED(gym)),
                              ("ask_owner", ASK_PAID(fine)), ("ask_owner", ASK_NOISE(tea))))

    assert await rig(world, script).run() == {"status": "completed"}

    assert [r["status"] for r in results(script.llm, "ask_owner")] == ["asked"] * 4, results(script.llm, "ask_owner")
    stored = {q.fact_id: q for q in await open_questions(world)}
    assert (stored[talk].question, stored[talk].texts, stored[talk].fallback, stored[talk].noise) == (
        "happened", TALK_TEXTS, "history", False)
    assert (stored[gym].question, stored[gym].texts, stored[gym].fallback, stored[gym].noise) == (
        "renewed", GYM_TEXTS, "history", False)
    assert (stored[fine].question, stored[fine].texts, stored[fine].fallback, stored[fine].noise) == (
        "paid", FINE_TEXTS, "history", False)
    assert (stored[tea].question, stored[tea].texts, stored[tea].fallback, stored[tea].noise) == (
        "still_true", {}, "archive", True)
    for folded in (train, hotel):
        assert world.docs[folded]["state"] == "superseded" and world.docs[folded]["superseded_by"] == talk
    script_spent(script.llm)


def test_the_scripted_calls_use_every_declared_field():
    updates = [REVIEW_REWRITE("f", "e"), REVIEW_HISTORY("f"), REVIEW_ARCHIVE("f", "q"), REVIEW_SUPERSEDE("f", "s"),
               REVIEW_REVIVE("f", "e")]
    asks = [ASK_HAPPENED("f", "a", "b"), ASK_RENEWED("f"), ASK_PAID("f"), ASK_NOISE("f")]
    assert unused("consolidation_review", "update_fact", updates) == []
    assert unused("consolidation_review", "ask_owner", asks) == []
