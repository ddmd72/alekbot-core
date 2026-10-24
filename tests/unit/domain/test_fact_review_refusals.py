"""What the review may change without a reason, and whom it may ask (Facts S4 · T1b2, plan §3.5)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import (
    REVIVE_NEEDS_BASIS,
    ask_refusal,
    basis_refusal,
    is_dated_plan,
)
from tests.support.review_rule_builders import NOW, fact, question

Q = QuestionStatus
TODAY = date(2026, 10, 24)
OTHER = lambda **o: fact("ev", lineage_id="L-ev", state=FactState.CURRENT, **o)  # noqa: E731
REPLIED = dict(reply_text="Yes, I still take it every morning", reply_at=NOW - timedelta(hours=5))


def refuse(action="revive", basis=None, *, subject=None, evidence=None, q=None, made_current=frozenset(), history=None,
           today=TODAY, tz="UTC"):
    return basis_refusal(
        action, subject or fact(), basis, evidence=evidence, question=q, made_current=made_current, history=history,
        today=today, tz=tz,
    )


def evidence_basis(fid="ev"):
    return ReviewBasis("evidence", evidence_fact_id=fid)


def reply_basis(quote="still take it", qid="q1"):
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
    assert text.startswith(f"basis: {action} needs a basis") and "evidence_fact_id" in text and "quote" in text
    assert ("date_passed" in text) is (action == "archive")


# ------------------------------------------------------------------------------------------------ evidence

@pytest.mark.parametrize("action", ["revive", "rewrite", "invalidate", "archive"])
def test_a_current_fact_of_another_lineage_is_evidence_for_every_action(action):
    assert refuse(action, evidence_basis(), evidence=OTHER()) is None


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
    assert refuse("revive", reply_basis("  STILL   take\nit "), q=q) is None


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


def test_a_question_without_a_recorded_reply_backs_nothing():
    assert "no recorded reply" in refuse("revive", reply_basis(), q=question(Q.ASKED))


# ------------------------------------------------------------------------------------------------ date_passed

def test_a_passed_plan_date_backs_an_archive_of_a_non_dated_fact():
    assert refuse("archive", ReviewBasis("date_passed", on=date(2026, 10, 23))) is None


@pytest.mark.parametrize("on", [TODAY, date(2026, 10, 25)])
def test_a_plan_date_of_today_or_later_has_not_passed(on):
    assert "not before today" in refuse("archive", ReviewBasis("date_passed", on=on))


def test_a_non_dated_fact_needs_the_date():
    assert refuse("archive", ReviewBasis("date_passed")).startswith("basis.date: the plan's date (YYYY-MM-DD)")


def test_a_dated_fact_uses_its_own_date_and_ignores_the_models():
    past = fact(**dated(date(2026, 10, 20)))
    assert refuse("archive", ReviewBasis("date_passed", on=date(2030, 1, 1)), subject=past) is None
    assert refuse("archive", ReviewBasis("date_passed"), subject=past) is None


@pytest.mark.parametrize("last_day", [TODAY, date(2026, 10, 30)])
def test_a_dated_fact_whose_date_is_today_or_later_is_not_over(last_day):
    assert "not before today" in refuse("archive", ReviewBasis("date_passed", on=date(2020, 1, 1)), subject=fact(**dated(last_day)))


def test_a_dated_fact_with_no_stored_date_has_no_passed_date():
    broken = fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=None)
    assert "unknown" in refuse("archive", ReviewBasis("date_passed", on=date(2020, 1, 1)), subject=broken)


def test_the_dated_facts_day_is_the_owners_not_utc():
    ends_late_utc = fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc))
    today = date(2026, 10, 24)
    assert "not before today" in refuse("archive", ReviewBasis("date_passed"), subject=ends_late_utc, today=today, tz="UTC")
    # 23:30 UTC is already 01:30 on the 25th in Madrid: the plan's last day is the 24th there as well, which is today
    assert "not before today" in refuse("archive", ReviewBasis("date_passed"), subject=ends_late_utc, today=today, tz="Europe/Madrid")
    over = fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc))
    assert refuse("archive", ReviewBasis("date_passed"), subject=over, today=date(2026, 10, 25), tz="UTC") is None


@pytest.mark.parametrize("action", ["revive", "rewrite", "invalidate"])
def test_a_passed_date_backs_only_an_archive(action):
    text = refuse(action, ReviewBasis("date_passed", on=date(2026, 10, 1)))
    assert "backs only an archive" in text and "date_passed" not in text.split("needs")[1]


# ------------------------------------------------------------------------------------------------ history

def test_history_goes_with_an_archive_on_a_reply_for_a_standing_fact():
    assert refuse("archive", reply_basis(), q=question(Q.ASKED, **REPLIED), history="No longer true: takes vitamin D") is None


@pytest.mark.parametrize("action", ["revive", "rewrite", "invalidate", "supersede"])
def test_history_with_any_other_action_is_refused(action):
    text = refuse(action, reply_basis(), q=question(Q.ASKED, **REPLIED), evidence=OTHER(), history="it ended")
    assert text is not None and "history goes only with an archive" in text


def test_history_on_an_archive_with_another_basis_is_refused():
    assert "history goes only" in refuse("archive", evidence_basis(), evidence=OTHER(), history="it ended")
    assert "history goes only" in refuse("archive", ReviewBasis("date_passed", on=date(2026, 10, 1)), history="it ended")


def test_history_on_a_dated_plan_is_refused_because_a_plan_that_is_over_is_archived_only():
    q = question(Q.ASKED, **REPLIED)
    assert "archived only" in refuse("archive", reply_basis(), q=q, subject=fact(**dated(date(2026, 10, 20))), history="it ended")
    assert "archived only" in refuse("archive", reply_basis(), q=question(Q.ASKED, fallback="archive", **REPLIED), history="it ended")


def test_blank_history_is_refused():
    assert "one sentence" in refuse("archive", reply_basis(), q=question(Q.ASKED, **REPLIED), history="  ")


def test_a_basis_problem_is_reported_before_a_history_problem():
    assert "no recorded reply" in refuse("archive", reply_basis(), q=question(Q.ASKED), history="it ended")


# ------------------------------------------------------------------------------------------------ is_dated_plan

def test_a_dated_plan_is_a_date_fact_or_one_that_falls_back_to_archive():
    assert is_dated_plan(fact(**dated(date(2026, 10, 1))))
    assert not is_dated_plan(fact())
    assert not is_dated_plan(fact(), question(fallback="revive"))
    assert is_dated_plan(fact(), question(fallback="archive"))
    assert is_dated_plan(fact(**dated(date(2026, 10, 1))), question(fallback="revive"))


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


@pytest.mark.parametrize("bad", ["", "ask", "REVIVE", "none"])
def test_the_fallback_is_revive_or_archive(bad):
    assert ask(bad) == "ask_owner.fallback: revive or archive"


def test_a_revive_fallback_takes_no_date_and_no_noise():
    assert "go with fallback archive only" in ask("revive", fallback_date=date(2026, 10, 1))
    assert "go with fallback archive only" in ask("revive", noise=True)


def test_archive_is_allowed_for_a_dated_plan():
    assert ask("archive", subject=fact(**dated(date(2026, 10, 30)))) is None


def test_archive_is_allowed_with_a_past_fallback_date_on_any_fact():
    assert ask("archive", fallback_date=date(2026, 10, 23)) is None


def test_archive_on_an_ordinary_fact_needs_a_date_or_noise():
    text = ask("archive")
    assert "only for a dated plan" in text and "use revive" in text


@pytest.mark.parametrize("given", [TODAY, date(2026, 10, 25)])
def test_a_fallback_date_of_today_or_later_is_refused_even_on_a_dated_plan(given):
    text = ask("archive", fallback_date=given, subject=fact(**dated(date(2026, 10, 30))))
    assert "must be before today" in text and TODAY.isoformat() in text


def test_today_is_the_owners_day():
    late = datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc)  # 23 Oct in UTC, 24 Oct 01:30 in Madrid
    assert "must be before today (2026-10-23)" in ask("archive", fallback_date=date(2026, 10, 23), now=late, tz="UTC")
    assert ask("archive", fallback_date=date(2026, 10, 22), now=late, tz="UTC") is None
    assert ask("archive", fallback_date=date(2026, 10, 23), now=late, tz="Europe/Madrid") is None
    assert "must be before today (2026-10-24)" in ask("archive", fallback_date=date(2026, 10, 24), now=late, tz="Europe/Madrid")
