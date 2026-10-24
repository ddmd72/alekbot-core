"""The owner's answer as a typed update (facts S4 · T3c, inward review L1): `answer_updates` gives a `ParsedFactUpdate`
whose expiry is an `Expiry` (`confirmed_expiry`), never model JSON; `stored_expiry` is the decision a stored fact holds,
the same one `expiry_view` shows the model; `history_refusal` judges the typed update."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactState, FactType
from src.domain.fact_expiry import EXPIRY_NONE, Expiry, expiry_view, parse_expiry, stored_expiry
from src.domain.fact_lifecycle import compute_expiry
from src.domain.fact_review_answer_change import AnswerChange
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import ARCHIVE_ONLY, answer_updates, ended_text, history_refusal
from src.domain.fact_review_reasons import answer_reason
from src.domain.fact_review_rules import confirmed_expiry, fallback_window_days
from src.domain.fact_update import ParsedFactUpdate
from tests.support.fact_write import ctx
from tests.support.review_rule_builders import NOW, fact, question

MADRID = "Europe/Madrid"
DAY = timedelta(days=1)
ENDS_28_OCT = compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 28)), NOW, MADRID)["expires_at"]


# ------------------------------------------------------------------------------------------------ answer_updates

def test_still_true_on_a_pinned_fact_is_a_current_update_with_expiry_none():
    change = answer_updates(AnswerKind.STILL_TRUE, fact(domain=FactDomain.AGENT_DIRECTIVE), question(), NOW, MADRID, "digit")

    assert change.update == ParsedFactUpdate(state=FactState.CURRENT, expiry=EXPIRY_NONE)
    assert isinstance(change.update.expiry, Expiry) and change.update.expiry.kind is ExpiryKind.NONE
    assert change.reason == answer_reason(AnswerKind.STILL_TRUE, question().question_id, "digit")
    assert change.evidence_at == NOW and change.history is None and change.fields is None


def test_still_true_keeps_a_date_ahead_as_a_typed_date_in_the_owners_zone():
    ahead = fact(state=FactState.CURRENT, expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=ENDS_28_OCT)

    change = answer_updates(AnswerKind.STILL_TRUE, ahead, question(), NOW, MADRID, "digit")

    assert change.update == ParsedFactUpdate(state=FactState.CURRENT, expiry=Expiry(ExpiryKind.DATE, on=date(2026, 10, 28)))


def test_still_true_on_a_due_horizon_is_a_typed_horizon_of_the_doubled_window():
    stale = fact()

    change = answer_updates(AnswerKind.STILL_TRUE, stale, question(), NOW, MADRID, "digit")

    assert change.update == ParsedFactUpdate(
        state=FactState.CURRENT, expiry=Expiry(ExpiryKind.HORIZON, days=fallback_window_days(stale))
    )


def test_ended_on_a_standing_fact_is_the_archive_alone_with_its_history_text():
    standing = fact(text="works at Acme")

    change = answer_updates(AnswerKind.ENDED, standing, question(), NOW, MADRID, "button")

    assert change.update == ARCHIVE_ONLY == ParsedFactUpdate(state=FactState.ARCHIVED)
    assert change.history == ended_text("works at Acme", NOW, MADRID)


def test_never_is_an_invalidation_and_nothing_else():
    change = answer_updates(AnswerKind.NEVER, fact(), question(), NOW, MADRID, "digit")

    assert change.update == ParsedFactUpdate(state=FactState.INVALIDATED) and change.history is None


def test_dont_know_stays_the_fallbacks_in_place_fields():
    change = answer_updates(AnswerKind.DONT_KNOW, fact(), question(), NOW, MADRID, "digit")

    assert change.update is None and change.fields is not None and "state" in change.fields


def test_an_answer_change_with_an_update_needs_its_reason_and_moment():
    with pytest.raises(ValueError):
        AnswerChange(update=ARCHIVE_ONLY, reason="", evidence_at=NOW)
    with pytest.raises(ValueError):
        AnswerChange(update=ARCHIVE_ONLY, reason="r")
    with pytest.raises(ValueError):
        AnswerChange(fields={"state": FactState.CURRENT}, update=ARCHIVE_ONLY)


# ------------------------------------------------------------------------------------------------ stored_expiry

@pytest.mark.parametrize("kind, ttl, at, tz, expected", [
    (ExpiryKind.NONE, None, None, MADRID, EXPIRY_NONE),
    # Reviewer edit (Facts S4 T6c, L4): the string-kind row ("none") is gone; stored_expiry / expiry_view take the enum,
    # and a stored string is decoded by the repository (test_fact_expiry_review_fixes, test_fact_view).
    (ExpiryKind.DATE, None, ENDS_28_OCT, MADRID, Expiry(ExpiryKind.DATE, on=date(2026, 10, 28))),
    (ExpiryKind.DATE, None, datetime(2026, 10, 29, 23, 30, tzinfo=timezone.utc), MADRID,
     Expiry(ExpiryKind.DATE, on=date(2026, 10, 30))),
    (ExpiryKind.HORIZON, 30, NOW, MADRID, Expiry(ExpiryKind.HORIZON, days=30)),
    (ExpiryKind.DATE, None, None, MADRID, None),
    (ExpiryKind.HORIZON, None, NOW, MADRID, None),
    (None, 30, NOW, MADRID, None),
])
def test_stored_expiry_is_the_decision_the_model_view_shows(kind, ttl, at, tz, expected):
    assert stored_expiry(kind, ttl, at, tz) == expected
    view, _ = expiry_view(kind, ttl, at, tz)
    assert (None if view is None else parse_expiry(view)) == expected


@pytest.mark.parametrize("make", [
    lambda: fact(domain=FactDomain.AGENT_DIRECTIVE),
    lambda: fact(type=FactType.PRINCIPLE),
    lambda: fact(state=FactState.CURRENT, expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None),
    lambda: fact(state=FactState.CURRENT, expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=ENDS_28_OCT),
    lambda: fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=NOW - DAY),
    fact,
])
def test_confirmed_expiry_is_typed(make):
    assert isinstance(confirmed_expiry(make(), NOW, MADRID), Expiry)


# ------------------------------------------------------------------------------------------------ history_refusal

@pytest.mark.parametrize("actor", ["review", "owner"])
def test_history_goes_with_the_archive_alone(actor):
    assert history_refusal("No longer true: x", ARCHIVE_ONLY, ctx(actor), NOW) is None


@pytest.mark.parametrize("update", [
    ParsedFactUpdate(state=FactState.INVALIDATED),
    ParsedFactUpdate(state=FactState.CURRENT, expiry=EXPIRY_NONE),
    ParsedFactUpdate(state=FactState.ARCHIVED, tags=["x"]),
    ParsedFactUpdate(state=FactState.ARCHIVED, notes=("a note",)),
    ParsedFactUpdate(),
])
def test_history_with_anything_but_the_archive_alone_is_refused(update):
    refusal = history_refusal("No longer true: x", update, ctx("owner"), NOW)
    assert refusal is not None and refusal.endswith("nothing was written")


def test_history_is_refused_for_a_consolidation_pass_an_empty_text_or_no_moment():
    assert "not stage1" in history_refusal("No longer true: x", ARCHIVE_ONLY, ctx("stage1"), NOW)
    assert "empty" in history_refusal("  ", ARCHIVE_ONLY, ctx("owner"), NOW)
    assert "evidence_at" in history_refusal("No longer true: x", ARCHIVE_ONLY, ctx("owner"), None)
