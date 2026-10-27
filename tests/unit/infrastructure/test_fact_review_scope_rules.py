"""`FactReviewScope` (Facts S4 · T6a2): the basis and ask rules wired into the scope (plan §3.5, `basis_refusal`,
`ask_refusal`), over the real repository, write service and adapter on the review world's evaluating Firestore fake."""
import asyncio
from datetime import timedelta

import pytest

from src.domain.entities import ExpiryKind
from src.domain.fact_review_basis import ReviewBasis, parse_review_basis
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reasons import basis_reason
from src.domain.fact_review_rules import REVIVE_NEEDS_BASIS
from src.domain.fact_tool_result import HISTORY_FACT_ID
from src.infrastructure.fact_review_scope import FactReviewScope
from src.domain.fact_review_batch import ReviewBatch
from tests.support.fact_kit import ACC, USER
from tests.support.fact_lifecycle_world import FUTURE, NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import question as build_question

TZ = "Europe/Lisbon"
HORIZON = {"kind": "horizon", "days": 30}
ARCHIVE = {"state": "archived"}
REPLY_AT = NOW - timedelta(hours=2)
# P2 revised (P2-1): a history rewrite of a plan whose date passed, in the "planned, unknown" phrasing
HISTORY_OF_ROME = {
    "state": "current", "expiry": {"kind": "none"}, "as_history": True,
    "content": "The user planned a trip to Rome in Oct 2026; whether it took place is unknown.",
}
ARCHIVE_NEEDS_REPLY = (
    "an archive needs the owner's reply ({kind: reply, question_id, quote}); a fact that is over is rewritten as history "
    "(as_history: true), or ask the owner with question happened"
)
ARCHIVE_ONLY_NOISE = (
    "ask_owner.fallback archive: only with noise: true; a plan whose date passed is asked with question happened and "
    "fallback history"
)
RETIRED = "ask_owner.fallback_date is retired: a plan whose date passed is asked with question happened and fallback history"
HAPPENED_TEXTS = {
    "happened": "The user went on a trip to Rome in Oct 2026.",
    "not_happened": "The user planned a trip to Rome in Oct 2026 and did not go.",
    "unknown": "The user planned a trip to Rome in Oct 2026; whether it took place is unknown.",
}
SHAPES = "{kind: evidence, evidence_fact_id}, {kind: reply, question_id, quote} or {kind: date_passed, date?}"


@pytest.fixture
def world():
    return ReviewWorld()


async def stale(world, text, **over):
    over.setdefault("expiry_kind", ExpiryKind.HORIZON)
    over.setdefault("ttl_days", 14)
    return await world.seed_stale(text, reported_date=NOW - timedelta(days=60), **over)


async def evidence(world, text="moved to Lisbon in October"):
    return await world.seed(text, reported_date=NOW - timedelta(days=1))


def asked(fact_id, question_id="q1", reply=None, **over):
    """An asked question about `fact_id`, sent three days before the world's now, with the owner's reply when given."""
    fields = dict(question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact_id, asked_at=NOW - timedelta(days=3))
    if reply is not None:
        fields.update(reply_text=reply, reply_at=REPLY_AT)
    fields.update(over)
    return build_question(QuestionStatus.ASKED, **fields)


async def scope_of(world, *fact_ids, questions=()):
    facts = tuple([await world.repo.get_fact_by_id(fact_id) for fact_id in fact_ids])
    return FactReviewScope(ReviewBatch(facts, tuple(questions)), ACC, TZ, world.now, world.repo, world.store.adapter)


def on(fact_id):
    return {"kind": "evidence", "evidence_fact_id": fact_id}


def on_reply(quote, question_id="q1"):
    return {"kind": "reply", "question_id": question_id, "quote": quote}


def state_of(world, fact_id):
    return world.docs[fact_id]["state"]


def last_reason(world, fact_id):
    return world.docs[fact_id]["lifecycle_events"][-1]["reason"]


# ------------------------------------------------------------------------------------------------ evidence and reply

async def test_the_facts_own_id_as_evidence_is_refused_with_the_shape_text(world):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": {"state": "current", "expiry": HORIZON}, "basis": on(fact)})

    assert res == {
        "fact_id": fact, "status": "failed", "refused": "basis",
        "message": "basis.evidence_fact_id is the fact's own lineage; another fact must say it",
    }
    assert state_of(world, fact) == "stale" and scope.decisions() == []


async def test_a_quote_not_in_the_reply_is_refused(world):
    fact = await world.seed("works at Acme")
    scope = await scope_of(world, fact, questions=[asked(fact, reply="I left Acme in September")])

    res = await scope.update_fact({"fact_id": fact, "updates": ARCHIVE, "basis": on_reply("I was fired")})

    assert res["refused"] == "basis" and res["message"] == "basis.quote is not in the owner's reply; copy words from it"
    assert state_of(world, fact) == "current"


async def test_a_quote_from_the_reply_archives_on_the_reply(world):
    fact = await world.seed("works at Acme")
    scope = await scope_of(world, fact, questions=[asked(fact, reply="I left Acme in September")])

    res = await scope.update_fact({"fact_id": fact, "updates": ARCHIVE, "basis": on_reply("left  ACME")})

    assert res["status"] == "updated", res
    assert state_of(world, fact) == "archived"
    assert last_reason(world, fact) == basis_reason("archive", ReviewBasis("reply", question_id="q1", quote="left ACME"))
    assert [(d.outcome, d.basis_kind) for d in scope.decisions()] == [("archived", "reply")]


async def test_a_follow_up_on_the_same_reply_after_a_rewrite_passes(world):
    fact = await world.seed("works at Acme")
    scope = await scope_of(world, fact, questions=[asked(fact, reply="I work at Globex now")])
    first = await scope.update_fact(
        {"fact_id": fact, "updates": {"content": "works at Globex", "expiry": HORIZON}, "basis": on_reply("at Globex")}
    )

    second = await scope.update_fact({
        "fact_id": first["fact_id"], "updates": {"tags": ["work", "globex"], "expiry": HORIZON},
        "basis": on_reply("at Globex"),
    })

    assert first["status"] == "updated" and first["fact_id"] != fact, first
    assert second["status"] == "updated" and second["fact_id"] != first["fact_id"], second
    assert world.docs[second["fact_id"]]["tags"] == ["work", "globex"]
    [decision] = scope.decisions()
    assert (decision.fact_id, decision.outcome, decision.basis_kind) == (fact, "rewritten", "reply")


async def test_a_reply_to_the_question_about_another_fact_is_refused(world):
    asked_about, other = await world.seed("works at Acme"), await stale(world, "b")
    scope = await scope_of(world, asked_about, other, questions=[asked(asked_about, reply="I work at Globex now")])

    res = await scope.update_fact({"fact_id": other, "updates": ARCHIVE, "basis": on_reply("at Globex")})

    assert res["refused"] == "basis"
    assert res["message"] == "basis.question_id: the id of the question about this fact in the batch"
    assert state_of(world, other) == "stale"


async def test_a_basis_kind_that_is_a_list_or_an_object_is_a_value_error_with_the_shape_text(world):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    for kind in (["evidence"], {"kind": "evidence"}):
        with pytest.raises(ValueError) as caught:
            parse_review_basis({"kind": kind})
        assert SHAPES in str(caught.value)
        res = await scope.update_fact({"fact_id": fact, "updates": ARCHIVE, "basis": {"kind": kind}})
        assert res["refused"] == "basis" and SHAPES in res["message"], res
    assert state_of(world, fact) == "stale"


# ------------------------------------------------------------------------------------------------ date passed

async def test_date_passed_with_a_past_date_rewrites_a_horizon_plan_as_history(world):
    # P2 revised (P2-1, P2-2): a passed date backs a history rewrite only; an archive on it is refused
    fact = await stale(world, "trip to Rome")
    scope = await scope_of(world, fact)
    passed = {"kind": "date_passed", "date": "2026-10-01"}

    archive = await scope.update_fact({"fact_id": fact, "updates": ARCHIVE, "basis": passed})
    res = await scope.update_fact({"fact_id": fact, "updates": HISTORY_OF_ROME, "basis": passed})

    assert archive["refused"] == "basis" and archive["message"] == ARCHIVE_NEEDS_REPLY
    assert res["status"] == "updated", res
    assert state_of(world, fact) == "superseded" and state_of(world, res["fact_id"]) == "current"
    assert world.docs[res["fact_id"]]["text"] == HISTORY_OF_ROME["content"]
    assert last_reason(world, res["fact_id"]) == "rewritten as history: date passed 2026-10-01"
    assert [(d.outcome, d.basis_kind, d.as_history) for d in scope.decisions()] == [("rewritten", "date_passed", True)]


async def test_date_passed_with_a_future_date_is_refused(world):
    fact = await stale(world, "trip to Rome")
    scope = await scope_of(world, fact)

    # P2 revised (P2-2): the date rule is read through the history rewrite a passed date backs
    res = await scope.update_fact({"fact_id": fact, "updates": HISTORY_OF_ROME, "basis": {"kind": "date_passed", "date": "2026-12-01"}})

    assert res["refused"] == "basis"
    assert res["message"] == "basis.date_passed: the plan's date 2026-12-01 is not before today (2026-10-23)"
    assert state_of(world, fact) == "stale"


# ------------------------------------------------------------------------------------------------ rewrite and revive

async def test_a_rewrite_without_a_basis_is_refused(world):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": {"content": "lives in Lisbon", "expiry": HORIZON}})

    assert res["refused"] == "basis"
    assert res["message"] == (
        "basis: rewrite needs a basis: {kind: evidence, evidence_fact_id} or {kind: reply, question_id, quote}"
    )
    assert state_of(world, fact) == "stale"


async def test_a_revive_with_a_horizon_and_no_basis_is_refused_with_ask_the_owner(world):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": {"state": "current", "expiry": HORIZON}})

    assert res == {"fact_id": fact, "status": "failed", "refused": "basis", "message": REVIVE_NEEDS_BASIS}
    assert "ask the owner instead" in res["message"] and state_of(world, fact) == "stale"


async def test_a_tag_only_change_on_a_stale_fact_is_a_rewrite(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)

    unbacked = await scope.update_fact({"fact_id": fact, "updates": {"tags": ["home"], "expiry": HORIZON}})
    res = await scope.update_fact({"fact_id": fact, "updates": {"tags": ["home"], "expiry": HORIZON}, "basis": on(ev)})

    assert unbacked["message"].startswith("basis: rewrite needs a basis"), unbacked
    assert res["status"] == "updated" and res["fact_id"] != fact, res
    assert state_of(world, fact) == "superseded" and world.docs[res["fact_id"]]["tags"] == ["home"]
    assert last_reason(world, res["fact_id"]) == basis_reason("rewrite", ReviewBasis("evidence", evidence_fact_id=ev))
    assert [d.outcome for d in scope.decisions()] == ["rewritten"]


async def test_evidence_may_name_a_batch_fact_this_pass_rewrote_by_its_batch_id(world):
    a, b, ev = await stale(world, "a"), await stale(world, "b"), await evidence(world)
    scope = await scope_of(world, a, b)
    rewritten = await scope.update_fact(
        {"fact_id": a, "updates": {"content": "lives in Lisbon", "expiry": HORIZON}, "basis": on(ev)}
    )

    res = await scope.update_fact({"fact_id": b, "updates": {"state": "invalidated"}, "basis": on(a)})

    assert rewritten["fact_id"] != a and state_of(world, a) == "superseded"
    assert res["status"] == "updated", res
    assert state_of(world, b) == "invalidated"
    assert last_reason(world, b) == basis_reason(
        "invalidate", ReviewBasis("evidence", evidence_fact_id=rewritten["fact_id"])
    )


async def test_a_stale_fact_is_no_evidence(world):
    a, b = await stale(world, "a"), await stale(world, "b")
    scope = await scope_of(world, a, b)

    res = await scope.update_fact({"fact_id": b, "updates": {"state": "invalidated"}, "basis": on(a)})

    assert res["refused"] == "basis" and res["message"] == "basis.evidence_fact_id names a fact that is stale, not current"


async def test_a_supersede_by_a_stale_batch_fact_is_refused(world):
    a, b = await stale(world, "a"), await stale(world, "b")
    scope = await scope_of(world, a, b)

    res = await scope.update_fact({"fact_id": b, "updates": {"state": "superseded", "superseded_by": a}})

    assert res["refused"] == "basis" and res["message"] == "superseded_by names a fact that is stale, not current"
    assert state_of(world, b) == "stale"


# ------------------------------------------------------------------------------------------------ history

async def test_history_on_a_dated_plan_is_refused(world):
    plan = await world.seed("flight to Rome on 1 November", expires_at=FUTURE)
    scope = await scope_of(world, plan, questions=[asked(plan, reply="that trip is over")])

    res = await scope.update_fact({
        "fact_id": plan, "updates": ARCHIVE, "basis": on_reply("trip is over"), "history": "Flew to Rome in 2026.",
    })

    # P2 revised (C5): a date fact that is over is rewritten as history, not archived beside a history fact
    assert res["refused"] == "basis" and res["message"] == (
        "history: a fact of expiry kind date is rewritten as history (as_history: true with content), not archived beside "
        "a history fact"
    )
    assert state_of(world, plan) == "current"


async def test_history_without_basis_reply_is_refused(world):
    fact, ev = await world.seed("works at Acme"), await evidence(world)
    scope = await scope_of(world, fact, questions=[asked(fact)])

    res = await scope.update_fact({
        "fact_id": fact, "updates": ARCHIVE, "basis": on(ev), "history": "Worked at Acme until October 2026.",
    })

    assert res["refused"] == "basis"
    # P2 revised (P2-4): an archive on evidence is refused before its history is read
    assert res["message"] == ARCHIVE_NEEDS_REPLY
    assert state_of(world, fact) == "current"


async def test_history_on_a_standing_fact_archives_it_beside_the_history_fact(world):
    fact = await world.seed("works at Acme")
    scope = await scope_of(world, fact, questions=[asked(fact, reply="no, I left Acme")])

    res = await scope.update_fact({
        "fact_id": fact, "updates": {**ARCHIVE, "history": "Worked at Acme until October 2026."},
        "basis": on_reply("I left Acme"),
    })

    assert res["status"] == "updated" and HISTORY_FACT_ID in res, res
    assert state_of(world, fact) == "archived"
    history = world.docs[res[HISTORY_FACT_ID]]
    assert history["text"] == "Worked at Acme until October 2026." and history["reported_date"] == REPLY_AT
    assert [d.outcome for d in scope.decisions()] == ["archived"]


async def test_a_history_that_is_not_text_is_refused_with_its_shape(world):
    fact = await world.seed("works at Acme")
    scope = await scope_of(world, fact, questions=[asked(fact, reply="no, I left Acme")])

    res = await scope.update_fact({
        "fact_id": fact, "updates": ARCHIVE, "basis": on_reply("I left Acme"), "history": ["ended"],
    })

    assert res["refused"] == "basis" and res["message"].startswith("history must be text: one sentence")


# ------------------------------------------------------------------------------------------------ asks

@pytest.mark.parametrize("args", [
    # P2 revised (P2-3): a past fallback_date is retired (refused below); a horizon plan is asked as happened instead
    {"question": "happened", "texts": HAPPENED_TEXTS, "fallback": "history"},
    {"fallback": "archive", "noise": True},
    {"fallback": "revive"},
])
async def test_an_archive_fallback_on_a_horizon_fact_needs_noise(world, args):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    res = await scope.ask_owner({"fact_id": fact, **args})

    assert res["status"] == "asked", res
    assert [a.fallback for a in scope.asks()] == [args["fallback"]]


@pytest.mark.parametrize("args, message", [
    # P2 revised (C5): the ask refusals' texts; fallback_date is retired, past or future
    ({"fallback": "archive"}, ARCHIVE_ONLY_NOISE),
    ({"fallback": "archive", "fallback_date": "2026-12-01"}, RETIRED),
    ({"fallback": "revive", "noise": True}, "ask_owner: noise goes with fallback archive only"),
    ({"fallback": "forget"}, "ask_owner.fallback: revive, archive or history"),
    ({"fallback": "archive", "fallback_date": "2026-10-01"}, RETIRED),
])
async def test_the_archive_fallback_rules_refuse_the_rest(world, args, message):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    res = await scope.ask_owner({"fact_id": fact, **args})

    assert res == {"fact_id": fact, "status": "failed", "refused": "ask", "message": message}
    assert scope.asks() == [] and [f.id for f in scope.kept()] == [fact]


async def test_an_archive_fallback_on_a_dated_plan_passes_with_noise(world):
    # P2 revised (P2-3): a date fact is asked as still_true only with archive and noise
    plan = await stale(world, "trip to Rome", expiry_kind=ExpiryKind.DATE, ttl_days=None)
    scope = await scope_of(world, plan)

    plain = await scope.ask_owner({"fact_id": plan, "fallback": "archive"})
    res = await scope.ask_owner({"fact_id": plan, "fallback": "archive", "noise": True})

    assert plain["refused"] == "ask" and plain["message"] == ARCHIVE_ONLY_NOISE
    assert res["status"] == "asked", res


async def test_a_fact_with_an_open_question_is_not_asked_again(world):
    fact = await world.seed("works at Acme")
    scope = await scope_of(world, fact, questions=[asked(fact, reply="hmm")])

    res = await scope.ask_owner({"fact_id": fact, "fallback": "revive"})

    assert res["refused"] == "ask" and res["message"] == "ask_owner: this fact already has an open question; leave it"
    assert scope.asks() == []


async def test_a_timed_out_candidate_is_not_asked_again(world):
    fact = await world.seed("works at Acme")
    timed_out = asked(fact, asked_at=NOW - timedelta(days=15))
    scope = await scope_of(world, fact, questions=[timed_out])

    res = await scope.ask_owner({"fact_id": fact, "fallback": "revive"})

    assert res["refused"] == "ask"
    assert res["message"] == "ask_owner: this fact's question timed out and its fallback applies; decide it or leave it"
    assert scope.asks() == [] and [f.id for f in scope.kept()] == [fact]


async def test_twenty_asks_in_one_pass_are_all_accepted(world):
    facts = [await stale(world, f"fact {n}") for n in range(20)]
    scope = await scope_of(world, *facts)

    results = await asyncio.gather(*(scope.ask_owner({"fact_id": f, "fallback": "revive"}) for f in facts))

    assert [r["status"] for r in results] == ["asked"] * 20
    assert [a.fact_id for a in scope.asks()] == facts and scope.kept() == []
