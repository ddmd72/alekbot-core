"""`FactReviewScope`, Facts P2 (card C): history rewrites (`as_history`, `payment`), the renewal expiry set by code, the
asks' kinds, texts and folds, and the texts the pass wrote. Over the review world's evaluating Firestore fake; assertions
are on the tool results, the scope's record and the raw stored documents."""
from datetime import timedelta

import pytest

from src.domain.entities import ExpiryKind
from src.domain.fact_review_batch import ReviewBatch
from src.domain.fact_review_invoice import InvoiceRequest
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import (
    ASK_OWNER_PLACE, NOT_EVIDENCE, UPDATE_FACT_PLACE, asked_refusal, confirmed_expiry, fold_refusal,
)
from src.infrastructure.fact_review_scope import RENEWAL_EXPIRY_NOTE, FactReviewScope
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import question as build_question

TZ = "Europe/Lisbon"
NONE = {"kind": "none"}
HISTORY_TEXT = "The user planned to attend a tennis lesson on Nov 4, 2026, and did not go."
HISTORY = {"state": "current", "content": HISTORY_TEXT, "expiry": NONE}
DATE_PASSED = {"kind": "date_passed"}
INVOICE_FACT = "Phone bill invoice of 42.50 EUR, due Oct 20, 2026"
INVOICE_TEXT = "An invoice for the phone bill (42.50 EUR), due Oct 20, 2026, was received; payment not confirmed."
OCT_20 = {"kind": "date_passed", "date": "2026-10-20"}
PAYMENT = {
    "paid": "The phone bill invoice (42.50 EUR), due Oct 20, 2026, was paid.",
    "not_paid": "The phone bill invoice (42.50 EUR), due Oct 20, 2026, was not paid.",
}
ASK_HAPPENED = {
    "happened": "The user attended a tennis lesson on Nov 4, 2026.",
    "not_happened": "The user planned a tennis lesson on Nov 4, 2026, and did not go.",
    "unknown": "The user planned a tennis lesson on Nov 4, 2026; whether it took place is unknown.",
}


@pytest.fixture
def world():
    return ReviewWorld()


async def dated(world, text="Tennis lesson on Nov 4, 2026"):
    return await world.seed_stale(text, expiry_kind=ExpiryKind.DATE)


async def horizon(world, text):
    return await world.seed_stale(
        text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14, reported_date=NOW - timedelta(days=60)
    )


async def scope_of(world, *fact_ids, questions=()):
    facts = tuple([await world.repo.get_fact_by_id(fact_id) for fact_id in fact_ids])
    return FactReviewScope(ReviewBatch(facts, tuple(questions)), ACC, TZ, world.now, world.repo, world.store.adapter)


def ask(fact_id, **over):
    return {"fact_id": fact_id, "question": "happened", "texts": ASK_HAPPENED, "fallback": "history", **over}


# ------------------------------------------------------------------------------------------------ as_history

@pytest.mark.parametrize("nested", [False, True])
async def test_as_history_beside_updates_or_inside_them_is_honoured(world, nested):
    fact = await dated(world)
    scope = await scope_of(world, fact)
    updates = {**HISTORY, "as_history": True} if nested else HISTORY
    args = {"fact_id": fact, "updates": updates, "basis": DATE_PASSED}
    if not nested:
        args["as_history"] = True

    res = await scope.update_fact(args)

    assert res["status"] == "updated", res
    [decision] = scope.decisions()
    assert (decision.outcome, decision.basis_kind, decision.as_history) == ("rewritten", "date_passed", True)


async def test_a_date_passed_history_is_written_as_a_version_and_recorded(world):
    fact = await dated(world)
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": HISTORY, "basis": DATE_PASSED, "as_history": True})

    assert res["status"] == "updated", res
    [decision] = scope.decisions()
    written = world.docs[decision.written_id]
    assert decision.written_id != fact and written["text"] == HISTORY_TEXT and written["state"] == "current"
    assert written["lineage_id"] == world.docs[fact]["lineage_id"]
    assert world.docs[fact]["state"] != "current"
    assert written["lifecycle_events"][-1]["reason"].startswith("rewritten as history: date passed ")
    assert scope.written_texts() == {f"{fact}:history": HISTORY_TEXT}


async def test_a_history_text_with_a_frame_character_is_still_a_rewrite_the_write_path_judges(world):
    fact = await dated(world)
    scope = await scope_of(world, fact)

    res = await scope.update_fact({
        "fact_id": fact, "updates": {"state": "current", "content": HISTORY_TEXT}, "basis": DATE_PASSED,
        "as_history": True,
    })

    assert res["refused"] == "scope" and "as_history rewrites the fact as history" in res["message"]
    assert scope.decisions() == [] and scope.written_texts() == {}


async def test_a_supersede_naming_a_date_passed_history_of_the_pass_is_refused(world):
    fact, other = await dated(world), await horizon(world, "Drinks green tea")
    scope = await scope_of(world, fact, other)
    first = await scope.update_fact({"fact_id": fact, "updates": HISTORY, "basis": DATE_PASSED, "as_history": True})
    written = scope.decisions()[0].written_id

    res = await scope.update_fact({"fact_id": other, "updates": {"state": "superseded", "superseded_by": written}})

    assert first["status"] == "updated"
    assert res["refused"] == "basis" and res["message"].endswith(NOT_EVIDENCE.split("}", 1)[1])
    assert world.docs[other]["state"] == "stale"


async def test_evidence_naming_a_date_passed_history_of_the_pass_is_refused(world):
    fact, other = await dated(world), await horizon(world, "Drinks green tea")
    scope = await scope_of(world, fact, other)
    await scope.update_fact({"fact_id": fact, "updates": HISTORY, "basis": DATE_PASSED, "as_history": True})
    written = scope.decisions()[0].written_id

    res = await scope.update_fact({
        "fact_id": other, "updates": {"state": "current", "expiry": {"kind": "horizon", "days": 30}},
        "basis": {"kind": "evidence", "evidence_fact_id": written},
    })

    assert res["refused"] == "basis" and res["message"].endswith(NOT_EVIDENCE.split("}", 1)[1])
    assert world.docs[other]["state"] == "stale"


# ------------------------------------------------------------------------------------------------ payment

async def test_a_payment_without_as_history_is_refused_and_nothing_is_written(world):
    fact, ev = await horizon(world, INVOICE_FACT), await world.seed("Paid the phone bill on Oct 21")
    scope = await scope_of(world, fact)

    res = await scope.update_fact({
        "fact_id": fact, "updates": {"state": "current", "expiry": {"kind": "horizon", "days": 30}},
        "basis": {"kind": "evidence", "evidence_fact_id": ev}, "payment": PAYMENT,
    })

    assert res["refused"] == "basis"
    assert res["message"] == "payment goes only with an as_history rewrite of an invoice that was received"
    assert scope.invoices() == [] and scope.written_texts() == {}


async def test_a_malformed_payment_is_a_basis_refusal(world):
    fact = await horizon(world, INVOICE_FACT)
    scope = await scope_of(world, fact)

    res = await scope.update_fact({
        "fact_id": fact, "updates": {**HISTORY, "content": INVOICE_TEXT}, "basis": OCT_20, "as_history": True,
        "payment": {"paid": "x"},
    })

    assert res["refused"] == "basis" and res["message"] == "payment must be {paid: <text>, not_paid: <text>}"


async def test_a_payment_with_as_history_makes_one_invoice_request_and_two_written_texts(world):
    fact = await horizon(world, INVOICE_FACT)
    scope = await scope_of(world, fact)

    res = await scope.update_fact({
        "fact_id": fact, "updates": {**HISTORY, "content": INVOICE_TEXT}, "basis": OCT_20, "as_history": True,
        "payment": PAYMENT,
    })

    assert res["status"] == "updated", res
    written = scope.decisions()[0].written_id
    assert scope.invoices() == [InvoiceRequest(fact, written, PAYMENT["paid"], PAYMENT["not_paid"])]
    assert scope.written_texts() == {
        f"{fact}:history": INVOICE_TEXT, f"{fact}:paid": PAYMENT["paid"], f"{fact}:not_paid": PAYMENT["not_paid"],
    }


async def test_a_later_write_without_payment_withdraws_the_invoice_and_its_texts(world):
    fact = await horizon(world, INVOICE_FACT)
    scope = await scope_of(world, fact)
    await scope.update_fact({
        "fact_id": fact, "updates": {**HISTORY, "content": INVOICE_TEXT}, "basis": OCT_20, "as_history": True,
        "payment": PAYMENT,
    })
    assert len(scope.invoices()) == 1

    res = await scope.update_fact({
        "fact_id": fact, "updates": {**HISTORY, "content": "The user paid the phone bill of 42.50 EUR on Oct 21, 2026."},
        "basis": OCT_20, "as_history": True,
    })

    assert res["status"] == "updated", res
    assert scope.invoices() == []
    assert set(scope.written_texts()) == {f"{fact}:history"}


# ------------------------------------------------------------------------------------------------ the renewal

async def test_the_expiry_of_a_renewal_on_a_reply_is_the_confirmed_window_set_by_code(world):
    fact = await horizon(world, "Gym membership valid until Oct 20, 2026")
    waiting = build_question(
        QuestionStatus.ASKED, question_id="q1", account_id=ACC, fact_id=fact, question="renewed",
        reply_text="renewed it", reply_at=NOW - timedelta(days=1), texts={"renewed": "x", "not_renewed": "y", "not_needed": "z", "unknown": "w"}, fallback="history",
    )
    scope = await scope_of(world, fact, questions=[waiting])
    stored = await world.repo.get_fact_by_id(fact)
    expected = confirmed_expiry(stored, world.now, TZ)

    res = await scope.update_fact({
        "fact_id": fact,
        "updates": {
            "state": "current", "content": "Gym membership valid until Oct 20, 2027",
            "expiry": {"kind": "horizon", "days": 3},
        },
        "basis": {"kind": "reply", "question_id": "q1", "quote": "renewed it"},
    })

    assert res["status"] == "updated", res
    assert expected.kind is ExpiryKind.HORIZON and expected.days != 3
    written = world.docs[scope.decisions()[0].written_id]
    control = await horizon(world, "Gym membership valid until Oct 20, 2026 (control)")
    control_scope = await scope_of(world, control)
    await control_scope.update_fact({
        "fact_id": control, "updates": {"state": "current", "expiry": {"kind": "horizon", "days": expected.days}},
        "basis": {"kind": "evidence", "evidence_fact_id": await world.seed("Still goes to the gym")},
    })
    sent_directly = world.docs[control_scope.decisions()[0].written_id]
    assert written["expires_at"].date() == sent_directly["expires_at"].date()  # the same window as when it is sent as such
    assert RENEWAL_EXPIRY_NOTE in str(res)


async def test_the_expiry_of_a_not_renewed_rewrite_with_kind_none_is_left_alone(world):
    fact = await horizon(world, "Gym membership valid until Oct 20, 2026")
    waiting = build_question(
        QuestionStatus.ASKED, question_id="q1", account_id=ACC, fact_id=fact, question="renewed",
        reply_text="renewed it", reply_at=NOW - timedelta(days=1), texts={"renewed": "x", "not_renewed": "y", "not_needed": "z", "unknown": "w"}, fallback="history",
    )
    scope = await scope_of(world, fact, questions=[waiting])

    res = await scope.update_fact({
        "fact_id": fact,
        "updates": {"state": "current", "content": "Gym membership ended on Oct 20, 2026", "expiry": NONE},
        "basis": {"kind": "reply", "question_id": "q1", "quote": "renewed it"},
    })

    assert res["status"] == "updated", res
    assert world.docs[scope.decisions()[0].written_id]["expiry_kind"] == "none"
    assert RENEWAL_EXPIRY_NOTE not in str(res)


# ------------------------------------------------------------------------------------------------ asks

async def test_an_accepted_ask_records_the_kind_the_texts_and_the_texts_the_pass_wrote(world):
    fact = await dated(world)
    scope = await scope_of(world, fact)

    res = await scope.ask_owner(ask(fact))

    assert res["status"] == "asked", res
    [request] = scope.asks()
    assert (request.fact_id, request.question, request.fallback, request.folds) == (fact, "happened", "history", ())
    assert request.texts == ASK_HAPPENED
    assert scope.written_texts() == {f"{fact}:{key}": text for key, text in ASK_HAPPENED.items()}


async def test_an_ask_with_the_wrong_texts_is_refused_and_holds_nothing(world):
    fact = await dated(world)
    scope = await scope_of(world, fact)

    res = await scope.ask_owner(ask(fact, texts={"happened": "x"}))

    assert res["refused"] == "ask"
    assert res["message"] == "ask_owner.texts: give exactly happened, not_happened, unknown for question happened"
    assert scope.asks() == [] and scope.written_texts() == {} and [f.id for f in scope.kept()] == [fact]


async def test_the_retired_fallback_date_is_refused(world):
    fact = await dated(world)
    scope = await scope_of(world, fact)

    res = await scope.ask_owner(ask(fact, fallback_date="2026-10-01"))

    assert res["refused"] == "ask" and res["message"].startswith("ask_owner.fallback_date is retired")


# ------------------------------------------------------------------------------------------------ folds

async def test_folds_are_held_with_the_ask_and_left_out_of_kept(world):
    flight, hotel, transfer, loose = [await horizon(world, t) for t in ("Flight", "Hotel", "Transfer", "Loose")]
    scope = await scope_of(world, flight, hotel, transfer, loose)

    res = await scope.ask_owner(ask(flight, folds=[hotel, transfer, hotel]))

    assert res["status"] == "asked", res
    [request] = scope.asks()
    assert request.folds == (hotel, transfer)
    assert [f.id for f in scope.kept()] == [loose]


async def test_a_write_to_a_fold_is_refused_as_asked(world):
    flight, hotel = await horizon(world, "Flight"), await horizon(world, "Hotel")
    scope = await scope_of(world, flight, hotel)
    await scope.ask_owner(ask(flight, folds=[hotel]))

    write = await scope.update_fact({"fact_id": hotel, "updates": HISTORY, "basis": DATE_PASSED, "as_history": True})
    second = await scope.ask_owner(ask(hotel))

    assert write["refused"] == "scope" and write["message"] == asked_refusal(UPDATE_FACT_PLACE)
    assert second["refused"] == "scope" and second["message"] == asked_refusal(ASK_OWNER_PLACE)
    assert world.docs[hotel]["state"] == "stale"


async def test_a_fold_that_is_the_asked_fact_is_refused(world):
    flight = await horizon(world, "Flight")
    scope = await scope_of(world, flight)

    res = await scope.ask_owner(ask(flight, folds=[flight]))

    assert res["refused"] == "ask" and res["message"] == fold_refusal(flight, "self")
    assert scope.asks() == []


async def test_a_fold_outside_the_batch_is_refused(world):
    flight, outside = await horizon(world, "Flight"), await horizon(world, "Elsewhere")
    scope = await scope_of(world, flight)

    res = await scope.ask_owner(ask(flight, folds=[outside]))

    assert res["refused"] == "ask" and res["message"] == fold_refusal(outside, "not_in_batch")
    assert scope.asks() == [] and [f.id for f in scope.kept()] == [flight]


async def test_a_fold_that_already_has_a_result_is_taken(world):
    flight, hotel = await horizon(world, "Flight"), await horizon(world, "Hotel")
    ev = await world.seed("Booked the hotel for good")
    scope = await scope_of(world, flight, hotel)
    await scope.update_fact({
        "fact_id": hotel, "updates": {"state": "current", "expiry": {"kind": "horizon", "days": 30}},
        "basis": {"kind": "evidence", "evidence_fact_id": ev},
    })

    res = await scope.ask_owner(ask(flight, folds=[hotel]))

    assert res["refused"] == "ask" and res["message"] == fold_refusal(hotel, "taken")
    assert scope.asks() == []


async def test_a_fold_that_is_already_asked_or_folded_is_taken(world):
    flight, hotel, transfer = [await horizon(world, t) for t in ("Flight", "Hotel", "Transfer")]
    scope = await scope_of(world, flight, hotel, transfer)
    await scope.ask_owner(ask(flight, folds=[hotel]))

    asked = await scope.ask_owner(ask(transfer, folds=[flight]))
    folded = await scope.ask_owner(ask(transfer, folds=[hotel]))

    assert asked["message"] == fold_refusal(flight, "taken")
    assert folded["message"] == fold_refusal(hotel, "taken")
    assert [a.fact_id for a in scope.asks()] == [flight]
    assert [f.id for f in scope.kept()] == [transfer]


async def test_a_fold_with_an_open_question_is_taken(world):
    flight, hotel = await horizon(world, "Flight"), await horizon(world, "Hotel")
    waiting = build_question(QuestionStatus.QUEUED, question_id="q1", account_id=ACC, fact_id=hotel)
    scope = await scope_of(world, flight, hotel, questions=[waiting])

    res = await scope.ask_owner(ask(flight, folds=[hotel]))

    assert res["message"] == fold_refusal(hotel, "taken") and scope.asks() == []


async def test_a_refused_fold_holds_none_of_the_others(world):
    flight, hotel, outside = await horizon(world, "Flight"), await horizon(world, "Hotel"), await horizon(world, "Out")
    scope = await scope_of(world, flight, hotel)

    res = await scope.ask_owner(ask(flight, folds=[hotel, outside]))

    assert res["refused"] == "ask"
    assert [f.id for f in scope.kept()] == [flight, hotel] and scope.written_texts() == {}
