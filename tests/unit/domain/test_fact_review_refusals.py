"""What the review may change without a reason, and whom it may ask (Facts S4 · T1b2, plan §3.5)."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.domain.entities import ExpiryKind, FactState, TemporalClass
from src.domain.fact_expiry import EXPIRY_NONE
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import (
    ARCHIVE_NEEDS_REPLY,
    DATED_PLAN_HISTORY,
    HISTORY_WITH_REWRITE,
    REVIVE_NEEDS_BASIS,
    ask_refusal,
    basis_refusal,
    is_dated_plan,
)
from tests.support.review_rule_builders import NOW, fact, question

Q = QuestionStatus
TODAY = date(2026, 10, 24)
OTHER = lambda **o: fact("ev", lineage_id="L-ev", state=FactState.CURRENT, **o)  # noqa: E731
REPLIED = dict(reply_text="Yes, I still drink it every morning", reply_at=NOW - timedelta(hours=5))


def update_for(action):
    """The update a review call of this action carries."""
    return {
        "revive": ParsedFactUpdate(state=FactState.CURRENT),
        "rewrite": ParsedFactUpdate(content="Drinks green tea daily", state=FactState.CURRENT),
        "archive": ParsedFactUpdate(state=FactState.ARCHIVED),
        "invalidate": ParsedFactUpdate(state=FactState.INVALIDATED),
        "supersede": ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="x"),
        # P2 revised (P2-1): a history rewrite, the one change a passed date backs
        "history": ParsedFactUpdate(
            content="The user drank green tea until Oct 2026.", state=FactState.CURRENT, expiry=EXPIRY_NONE,
            as_history=True,
        ),
    }[action]


def refuse(action="revive", basis=None, *, subject=None, evidence=None, q=None, made_current=frozenset(), history=None,
           today=TODAY, tz="UTC"):
    return basis_refusal(
        subject or fact(), update_for(action), basis, named_fact=evidence, question=q, made_current=made_current,
        history=history, today=today, tz=tz,
    )


def evidence_basis(fid="ev"):
    return ReviewBasis("evidence", evidence_fact_id=fid)


def reply_basis(quote="still drink it", qid="q1"):
    return ReviewBasis("reply", question_id=qid, quote=quote)


def dated(day: date, tz_midnight=timezone.utc):
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz_midnight)
    return dict(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=end)


# ------------------------------------------------------------------------------------------------ no basis at all

def test_a_revive_with_no_basis_is_told_to_ask_the_owner():
    assert refuse("revive") == REVIVE_NEEDS_BASIS and "ask the owner instead" in REVIVE_NEEDS_BASIS


@pytest.mark.parametrize("action", ["rewrite", "invalidate", "archive"])
def test_rewrite_invalidate_and_archive_with_no_basis_are_refused_with_the_shapes(action):
    text = refuse(action)
    assert text.startswith(f"basis: {action} needs a basis") and "quote" in text
    # P2 revised (P2-2, C5): an archive needs the owner's reply only; no shape names date_passed but a history rewrite's
    assert ("evidence_fact_id" in text) is (action != "archive")
    assert "date_passed" not in text


# ------------------------------------------------------------------------------------------------ evidence

@pytest.mark.parametrize("action", ["revive", "rewrite", "invalidate", "archive"])
def test_a_current_fact_of_another_lineage_is_evidence_for_every_action(action):
    # P2 revised (P2-2): evidence backs every action but an archive, which needs the owner's reply
    expected = ARCHIVE_NEEDS_REPLY if action == "archive" else None
    assert refuse(action, evidence_basis(), evidence=OTHER()) == expected


def test_evidence_needs_an_id():
    assert "basis.evidence_fact_id: the id" in refuse("revive", ReviewBasis("evidence"), evidence=OTHER())
    assert "basis.evidence_fact_id: the id" in refuse("revive", ReviewBasis("evidence", evidence_fact_id=""), evidence=OTHER())


def test_evidence_that_was_not_found_is_refused():
    assert "names no fact of this account" in refuse("revive", evidence_basis(), evidence=None)


def test_evidence_of_another_account_is_refused():
    assert "names no fact of this account" in refuse("revive", evidence_basis(), evidence=OTHER(account_id="other"))


def test_the_facts_own_lineage_is_not_evidence_for_itself():
    own = fact("f1-v2", lineage_id=fact().lineage_id, state=FactState.CURRENT)
    assert "own lineage" in refuse("revive", evidence_basis("f1-v2"), evidence=own)


@pytest.mark.parametrize("closed", [FactState.STALE, FactState.ARCHIVED, FactState.INVALIDATED, FactState.SUPERSEDED])
def test_evidence_must_be_current(closed):
    text = refuse("revive", evidence_basis(), evidence=fact("ev", lineage_id="L-ev", state=closed))
    assert closed.value in text and "not current" in text


def test_a_fact_this_pass_made_current_is_evidence_even_if_read_as_stale():
    stale = fact("ev", lineage_id="L-ev", state=FactState.STALE)
    assert refuse("revive", evidence_basis(), evidence=stale, made_current={"ev"}) is None
    assert refuse("revive", evidence_basis(), evidence=stale, made_current={"other"}) is not None


# ------------------------------------------------------------------------------------------------ supersede

def test_a_supersede_names_a_current_successor_and_needs_no_basis():
    assert refuse("supersede", None, evidence=OTHER()) is None
    assert refuse("supersede", reply_basis(), evidence=OTHER()) is None  # a basis is not looked at


def test_a_supersede_without_a_usable_successor_is_refused_with_its_own_place():
    assert refuse("supersede", evidence=None).startswith("superseded_by")
    assert "own lineage" in refuse("supersede", evidence=fact("x", lineage_id=fact().lineage_id, state=FactState.CURRENT))
    assert "not current" in refuse("supersede", evidence=fact("x", lineage_id="L-x", state=FactState.ARCHIVED))
    assert refuse("supersede", evidence=fact("x", lineage_id="L-x", state=FactState.STALE), made_current={"x"}) is None


# ------------------------------------------------------------------------------------------------ reply

def test_a_quote_found_in_the_owners_reply_is_a_basis():
    q = question(Q.ASKED, **REPLIED)
    for action in ("revive", "rewrite", "invalidate", "archive"):
        assert refuse(action, reply_basis(), q=q) is None


def test_the_quote_may_differ_in_case_and_spacing():
    q = question(Q.ASKED, **REPLIED)
    assert refuse("revive", reply_basis("  STILL   drink\nit "), q=q) is None


def test_a_quote_not_in_the_reply_is_refused():
    assert "not in the owner's reply" in refuse("revive", reply_basis("never again"), q=question(Q.ASKED, **REPLIED))


@pytest.mark.parametrize("quote", [None, "", "   "])
def test_a_reply_basis_needs_words_to_quote(quote):
    assert "basis.quote" in refuse("revive", ReviewBasis("reply", question_id="q1", quote=quote), q=question(Q.ASKED, **REPLIED))


def test_a_reply_basis_needs_a_question_of_this_fact_in_the_batch():
    asked = question(Q.ASKED, **REPLIED)
    assert "basis.question_id" in refuse("revive", reply_basis(), q=None)
    assert "basis.question_id" in refuse("revive", ReviewBasis("reply", quote="x"), q=asked)
    assert "basis.question_id" in refuse("revive", reply_basis(), q=question(Q.ASKED, fact_id="other", **REPLIED))


def test_the_question_must_be_the_one_the_basis_names():
    other = question(Q.ASKED, question_id="q2", **REPLIED)
    assert "basis.question_id" in refuse("revive", reply_basis(qid="q1"), q=other)
    assert refuse("revive", reply_basis(qid="q2"), q=other) is None


def test_a_question_without_a_recorded_reply_backs_nothing():
    assert "no recorded reply" in refuse("revive", reply_basis(), q=question(Q.ASKED))


# ------------------------------------------------------------------------------------------------ date_passed

# P2 revised (P2-2): a passed date backs only a history rewrite; the date rules below are unchanged and are now read
# through that rewrite ("history" in update_for), where they were read through an archive.

def test_a_passed_plan_date_backs_a_history_rewrite_not_an_archive_of_a_non_dated_fact():
    # P2 revised (P2-4): an archive on a passed date is refused; the same date backs the history rewrite
    assert refuse("archive", ReviewBasis("date_passed", on=date(2026, 10, 23))) == ARCHIVE_NEEDS_REPLY
    assert refuse("history", ReviewBasis("date_passed", on=date(2026, 10, 23))) is None


@pytest.mark.parametrize("on", [TODAY, date(2026, 10, 25)])
def test_a_plan_date_of_today_or_later_has_not_passed(on):
    assert "not before today" in refuse("history", ReviewBasis("date_passed", on=on))


def test_a_non_dated_fact_needs_the_date():
    assert refuse("history", ReviewBasis("date_passed")).startswith("basis.date: the plan's date (YYYY-MM-DD)")


def test_a_dated_fact_uses_its_own_date_and_ignores_the_models():
    past = fact(**dated(date(2026, 10, 20)))
    assert refuse("history", ReviewBasis("date_passed", on=date(2030, 1, 1)), subject=past) is None
    assert refuse("history", ReviewBasis("date_passed"), subject=past) is None


@pytest.mark.parametrize("last_day", [TODAY, date(2026, 10, 30)])
def test_a_dated_fact_whose_date_is_today_or_later_is_not_over(last_day):
    assert "not before today" in refuse("history", ReviewBasis("date_passed", on=date(2020, 1, 1)), subject=fact(**dated(last_day)))


def test_a_dated_fact_with_no_stored_date_has_no_passed_date():
    broken = fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=None)
    assert "unknown" in refuse("history", ReviewBasis("date_passed", on=date(2020, 1, 1)), subject=broken)


def test_the_dated_facts_day_is_the_owners_not_utc():
    # a plan for the 24th, stored as the owner's local midnight that ends it
    ny = fact(**dated(date(2026, 10, 24), ZoneInfo("America/New_York")))  # 25 Oct 04:00 UTC
    assert ny.expires_at == datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)
    on_the_25th = date(2026, 10, 25)
    assert refuse("history", ReviewBasis("date_passed"), subject=ny, today=on_the_25th, tz="America/New_York") is None
    # read in UTC the plan would end on the 25th, which is not before the 25th
    assert "not before today" in refuse("history", ReviewBasis("date_passed"), subject=ny, today=on_the_25th, tz="UTC")
    # and on the 24th, New York time, it has not passed
    assert "not before today" in refuse("history", ReviewBasis("date_passed"), subject=ny, today=date(2026, 10, 24), tz="America/New_York")


def test_a_plan_east_of_utc_is_the_same_day_in_both_readings():
    madrid = fact(**dated(date(2026, 10, 24), ZoneInfo("Europe/Madrid")))  # 24 Oct 22:00 UTC
    assert refuse("history", ReviewBasis("date_passed"), subject=madrid, today=date(2026, 10, 25), tz="Europe/Madrid") is None
    assert "not before today" in refuse("history", ReviewBasis("date_passed"), subject=madrid, today=date(2026, 10, 24), tz="Europe/Madrid")


@pytest.mark.parametrize("action", ["revive", "rewrite", "invalidate"])
def test_a_passed_date_backs_only_a_history_rewrite(action):
    text = refuse(action, ReviewBasis("date_passed", on=date(2026, 10, 1)))
    assert "backs only a history rewrite" in text and "date_passed" not in text.split("needs")[1]


# ------------------------------------------------------------------------------------------------ history

def test_history_goes_with_an_archive_on_a_reply_for_a_standing_fact():
    assert refuse("archive", reply_basis(), q=question(Q.ASKED, **REPLIED), history="No longer true: drinks green tea") is None


@pytest.mark.parametrize("action", ["revive", "rewrite", "invalidate", "supersede"])
def test_history_with_any_other_action_is_refused(action):
    text = refuse(action, reply_basis(), q=question(Q.ASKED, **REPLIED), evidence=OTHER(), history="it ended")
    # P2 revised (C5): a rewrite sent with history is told to send as_history instead
    if action == "rewrite":
        assert text == HISTORY_WITH_REWRITE
    else:
        assert text is not None and "history goes only with an archive" in text


def test_history_on_an_archive_with_another_basis_is_refused():
    # P2 revised (P2-4): an archive on any basis but a reply is refused before its history is read
    assert refuse("archive", evidence_basis(), evidence=OTHER(), history="it ended") == ARCHIVE_NEEDS_REPLY
    assert refuse("archive", ReviewBasis("date_passed", on=date(2026, 10, 1)), history="it ended") == ARCHIVE_NEEDS_REPLY


def test_history_on_a_dated_plan_is_refused_because_a_plan_that_is_over_is_rewritten_as_history():
    # P2 revised (C5, C7): a date fact that is over is rewritten as history, not archived beside a history fact; a fact
    # whose question falls back to archive is noise and is archived only (arbiter: archive only for noise and never-true)
    q = question(Q.ASKED, **REPLIED)
    assert refuse("archive", reply_basis(), q=q, subject=fact(**dated(date(2026, 10, 20))), history="it ended") == DATED_PLAN_HISTORY
    assert "archived only" in refuse("archive", reply_basis(), q=question(Q.ASKED, fallback="archive", **REPLIED), history="it ended")


def test_blank_history_is_refused():
    assert "one sentence" in refuse("archive", reply_basis(), q=question(Q.ASKED, **REPLIED), history="  ")


def test_a_basis_problem_is_reported_before_a_history_problem():
    assert "no recorded reply" in refuse("archive", reply_basis(), q=question(Q.ASKED), history="it ended")


# ------------------------------------------------------------------------------------------------ is_dated_plan

def test_a_dated_plan_is_a_date_fact_only():
    # P2 revised (C7): is_dated_plan reads the fact's expiry kind only; its question parameter is gone
    assert is_dated_plan(fact(**dated(date(2026, 10, 1))))
    assert not is_dated_plan(fact())
    assert not is_dated_plan(fact(expiry_kind=ExpiryKind.HORIZON))


# ------------------------------------------------------------------------------------------------ ask_refusal

def ask(fallback="revive", fallback_date=None, noise=False, open_question=None, subject=None, now=NOW, tz="UTC"):
    return ask_refusal(subject or fact(), fallback, fallback_date, noise, open_question, now, tz)


def test_a_fact_with_nothing_open_may_be_asked_with_either_fallback_that_fits():
    assert ask("revive") is None
    assert ask("archive", noise=True) is None


@pytest.mark.parametrize("status", [Q.QUEUED, Q.PENDING, Q.ASKED, Q.UNANSWERED])
def test_a_fact_with_an_open_question_is_not_asked_again(status):
    assert "already has an open question" in ask(open_question=question(status))


def test_a_timed_out_question_says_its_fallback_applies():
    text = ask(open_question=question(Q.ASKED, asked_at=NOW - timedelta(days=14)))
    assert "timed out" in text and "fallback applies" in text
    assert "timed out" not in ask(open_question=question(Q.ASKED, asked_at=NOW - timedelta(days=14) + timedelta(seconds=1)))


# P2 revised (P2-3, C5): history joins the fallbacks, archive goes only with noise, and fallback_date is retired for
# every new ask (it existed to archive a horizon plan whose date passed).
RETIRED = "ask_owner.fallback_date is retired: a plan whose date passed is asked with question happened and fallback history"
ARCHIVE_ONLY_NOISE = (
    "ask_owner.fallback archive: only with noise: true; a plan whose date passed is asked with question happened and "
    "fallback history"
)


@pytest.mark.parametrize("bad", ["", "ask", "REVIVE", "none"])
def test_the_fallback_is_revive_archive_or_history(bad):
    assert ask(bad) == "ask_owner.fallback: revive, archive or history"


def test_a_revive_fallback_takes_no_date_and_no_noise():
    assert ask("revive", fallback_date=date(2026, 10, 1)) == RETIRED
    assert ask("revive", noise=True) == "ask_owner: noise goes with fallback archive only"


def test_archive_on_a_dated_plan_needs_noise():
    assert ask("archive", subject=fact(**dated(date(2026, 10, 30)))) == ARCHIVE_ONLY_NOISE
    assert ask("archive", noise=True, subject=fact(**dated(date(2026, 10, 30)))) is None


def test_a_past_fallback_date_is_retired_on_any_fact():
    assert ask("archive", fallback_date=date(2026, 10, 23)) == RETIRED


def test_archive_on_an_ordinary_fact_needs_noise():
    assert ask("archive") == ARCHIVE_ONLY_NOISE


@pytest.mark.parametrize("given", [TODAY, date(2026, 10, 25)])
def test_a_fallback_date_of_today_or_later_is_refused_even_on_a_dated_plan(given):
    assert ask("archive", fallback_date=given, subject=fact(**dated(date(2026, 10, 30)))) == RETIRED


def test_a_fallback_date_is_retired_whatever_the_owners_day():
    # no ask rule reads the day any more: a fallback date is retired whatever the day and the owner's zone
    late = datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc)  # 23 Oct in UTC, 24 Oct 01:30 in Europe/Madrid
    for given in (date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 24)):
        for tz in ("UTC", "Europe/Madrid"):
            assert ask("archive", fallback_date=given, now=late, tz=tz) == RETIRED
