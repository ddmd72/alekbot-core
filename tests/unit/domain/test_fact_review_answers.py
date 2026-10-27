"""The owner's answers (Facts S4 · T1c, plan §3.16): which reply is a digit answer, what each answer does to its fact,
and the text of the history fact an "ended" leaves behind."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactState, FactType
from src.domain.fact_lifecycle import compute_expiry
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_expiry import Expiry
from src.domain.fact_review_answer_change import AnswerChange
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import ARCHIVE_ONLY, answer_updates, digit_answer, ended_text
from src.domain.fact_review_options import OPTIONS
from src.domain.fact_review_reasons import answer_reason, is_revive_reason
from src.domain.fact_review_rules import fallback_fields, fallback_window_days
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from tests.support.review_rule_builders import NOW, fact, question
from tests.unit.domain.review_p2_texts import TEXTS

DAY = timedelta(days=1)
# Reviewer edit (facts S4, T3c): an answer carries a typed update (ParsedFactUpdate, an Expiry), not the model's argument
# JSON; each old dict is asserted as the typed value it parsed to, compared whole.
ARCHIVE = ParsedFactUpdate(state=FactState.ARCHIVED)
INVALIDATE = ParsedFactUpdate(state=FactState.INVALIDATED)


def confirm(expiry: Expiry) -> ParsedFactUpdate:
    return ParsedFactUpdate(state=FactState.CURRENT, expiry=expiry)
MADRID = "Europe/Madrid"
# a plan whose last day is 28 Oct in Europe/Madrid: it is due at the start of the next local day
ENDS_28_OCT = compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 28)), NOW, MADRID)["expires_at"]


def message(ids=("still_true", "ended", "never", "dont_know")) -> InteractiveMessage:
    return InteractiveMessage(
        prompt="Is this still true?",
        options=tuple(InteractiveOption(id=i, label=i) for i in ids),
        binding=InteractiveBinding(kind="fact_question", id="q1"),
    )


# ------------------------------------------------------------------------------------------------ the kinds

def test_the_four_kinds_are_the_four_option_ids_in_order():
    # P2 revised (C2): the four S4 kinds are the still_true question's options, in order; the seven new kinds follow
    assert [kind.value for kind in OPTIONS["still_true"]] == ["still_true", "ended", "never", "dont_know"]
    assert [kind.value for kind in AnswerKind] == [
        "still_true", "ended", "never", "dont_know",
        "happened", "not_happened", "renewed", "not_renewed", "not_needed", "paid", "not_paid",
    ]


# ------------------------------------------------------------------------------------------------ digit_answer

@pytest.mark.parametrize("text, kind", [
    ("1", AnswerKind.STILL_TRUE), ("2", AnswerKind.ENDED), ("3", AnswerKind.NEVER), ("4", AnswerKind.DONT_KNOW),
])
def test_a_digit_is_the_answer_of_its_option(text, kind):
    assert digit_answer(message(), text) is kind


@pytest.mark.parametrize("text, kind", [
    (" 2 ", AnswerKind.ENDED), ("\n3\n", AnswerKind.NEVER), ("2.", AnswerKind.ENDED), (" 2. ", AnswerKind.ENDED),
    ("`3`", AnswerKind.NEVER), (" `3` ", AnswerKind.NEVER), ("` 3 `", AnswerKind.NEVER), ("`3.`", AnswerKind.NEVER),
    ("1 .", AnswerKind.STILL_TRUE), ("``3``", AnswerKind.NEVER), ("```\n4\n```", AnswerKind.DONT_KNOW),
])
def test_the_punctuation_a_chat_client_puts_around_a_lone_digit_is_not_words(text, kind):
    assert digit_answer(message(), text) is kind


@pytest.mark.parametrize("text", [
    "0", "5", "12", "11", "21", "-1", "1.5", "1,", "(1)", "1!", "1)", "2 уже нет", "уже нет 2", "да", "yes", "Yes 1",
    "one", "", " ", "\n", ".", "..", "`", "``", "```", "`1", "1`", "`3`.", "1..", "1 2", "١", "２",
])
def test_anything_but_a_lone_digit_is_words_and_words_are_the_reviews(text):
    assert digit_answer(message(), text) is None


def test_the_digit_is_the_options_position_in_the_message_not_the_kinds_order():
    swapped = message(("ended", "still_true", "never", "dont_know"))
    assert digit_answer(swapped, "1") is AnswerKind.ENDED
    assert digit_answer(swapped, "2") is AnswerKind.STILL_TRUE


def test_a_digit_beyond_the_options_offered_is_words():
    two = message(("still_true", "ended"))
    assert digit_answer(two, "2") is AnswerKind.ENDED
    assert digit_answer(two, "3") is None
    assert digit_answer(two, "4") is None


def test_an_option_that_is_not_an_answer_is_a_bug_not_words():
    with pytest.raises(ValueError):
        digit_answer(message(("still_true", "maybe")), "2")


# ------------------------------------------------------------------------------------------------ ended_text

def test_ended_text_says_who_said_it_when_and_what_it_was():
    at = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
    assert ended_text("Drinks green tea", at, MADRID) == "No longer true (the owner said so in Oct 2026): Drinks green tea"


def test_the_month_is_the_owners_not_utcs():
    late = datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc)  # already 1 Nov in Europe/Madrid
    assert ended_text("x", late, MADRID).startswith("No longer true (the owner said so in Nov 2026)")
    assert ended_text("x", late, "UTC").startswith("No longer true (the owner said so in Oct 2026)")
    assert ended_text("x", late, "America/Los_Angeles").startswith("No longer true (the owner said so in Oct 2026)")


def test_the_year_is_the_owners_too():
    new_year = datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)
    assert "in Jan 2027)" in ended_text("x", new_year, MADRID)
    assert "in Dec 2026)" in ended_text("x", new_year, "UTC")


def test_ended_text_keeps_the_content_as_stored():
    assert ended_text("Живе в Лісабоні — «стара адреса»", NOW, MADRID).endswith(": Живе в Лісабоні — «стара адреса»")


def test_a_naive_moment_is_read_as_utc():
    assert "Nov 2026" in ended_text("x", datetime(2026, 10, 31, 23, 30), MADRID)
    assert "Oct 2026" in ended_text("x", datetime(2026, 10, 31, 23, 30), "UTC")


# ------------------------------------------------------------------------------------------------ still_true

def test_still_true_brings_the_fact_back_for_the_window_a_silence_would_give():
    stale = fact()  # reported 60 days ago with a 14-day horizon
    change = answer_updates(AnswerKind.STILL_TRUE, stale, question(), NOW, MADRID, "digit")
    assert change.update == confirm(Expiry(ExpiryKind.HORIZON, days=28))
    assert change.update.expiry.days == fallback_window_days(stale)
    assert (change.fields, change.history) == (None, None)


def test_still_true_asks_the_window_from_the_rule_not_from_a_copy_of_it():
    stale = fact()
    first = fallback_fields(stale, question(), NOW - 30 * DAY, "timeout")  # a silence long ago: 28 days
    revived = stale.model_copy(update=first)
    assert fallback_window_days(revived) == 56
    change = answer_updates(AnswerKind.STILL_TRUE, revived, question(), NOW, MADRID, "digit")
    assert change.update.expiry == Expiry(ExpiryKind.HORIZON, days=56)


def test_still_true_is_the_horizon_the_revive_fallback_would_give():
    stale = fact()
    days = answer_updates(AnswerKind.STILL_TRUE, stale, question(), NOW, MADRID, "digit").update.expiry.days
    due = fallback_fields(stale, question(), NOW, "owner")["expires_at"]
    assert days * DAY <= due - NOW < (days + 1) * DAY


@pytest.mark.parametrize("source", ["digit", "button", "cabinet"])
def test_still_true_records_the_answer_as_the_owners_with_its_source_and_counts_as_a_revive(source):
    change = answer_updates(AnswerKind.STILL_TRUE, fact(), question(question_id="q9"), NOW, MADRID, source)
    assert change.reason == answer_reason(AnswerKind.STILL_TRUE, "q9", source) == f"confirmed q=q9 ({source})"
    assert is_revive_reason(change.reason)


def test_still_true_on_a_waiting_current_fact_is_the_same_write():
    waiting = fact(state=FactState.CURRENT, stale_since=None)
    assert answer_updates(AnswerKind.STILL_TRUE, waiting, question(), NOW, MADRID, "digit").update == confirm(
        Expiry(ExpiryKind.HORIZON, days=28)
    )


# ------------------------------------------------------------------------------------------------ ended

def test_ended_on_a_standing_fact_archives_it_and_leaves_a_history_fact():
    change = answer_updates(AnswerKind.ENDED, fact(), question(), NOW, MADRID, "digit")
    assert change.update == ARCHIVE == ARCHIVE_ONLY
    assert change.history == ended_text("Drinks green tea", NOW, MADRID) == (
        "No longer true (the owner said so in Oct 2026): Drinks green tea"
    )
    assert change.reason == "ended q=q1 (digit)"
    assert not is_revive_reason(change.reason)


def test_the_history_text_is_the_facts_own_and_its_month_the_owners():
    late = datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc)
    change = answer_updates(AnswerKind.ENDED, fact(text="Lives in Lisbon"), question(), late, MADRID, "button")
    assert change.history == "No longer true (the owner said so in Nov 2026): Lives in Lisbon"


@pytest.mark.parametrize("standing", [
    {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None},
    {"expiry_kind": None, "ttl_days": None, "expires_at": None},
    {},
])
def test_ended_on_a_horizon_none_or_unclassified_fact_is_a_standing_fact(standing):
    change = answer_updates(AnswerKind.ENDED, fact(**standing), question(), NOW, MADRID, "digit")
    assert change.history is not None


def test_ended_on_a_dated_plan_archives_only():
    plan = fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=NOW - 3 * DAY)
    change = answer_updates(AnswerKind.ENDED, plan, question(), NOW, MADRID, "digit")
    assert change.update == ARCHIVE
    assert change.history is None


def test_ended_on_a_fact_whose_question_falls_back_to_archive_archives_only():
    # Arbiter (owner's rule: archive only for noise and never-true): a question that falls back to archive asks about
    # noise, and noise is archived with no history fact
    change = answer_updates(AnswerKind.ENDED, fact(), question(fallback="archive"), NOW, MADRID, "cabinet")
    assert change.update == ARCHIVE
    assert change.history is None
    assert change.reason == "ended q=q1 (cabinet)"


# ------------------------------------------------------------------------------------------------ never

@pytest.mark.parametrize("make", [fact, lambda: fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=NOW - DAY)])
def test_never_invalidates_and_writes_nothing_new(make):
    change = answer_updates(AnswerKind.NEVER, make(), question(fallback="archive"), NOW, MADRID, "button")
    assert change.update == INVALIDATE
    assert change.history is None
    assert change.reason == "never q=q1 (button)"
    assert not is_revive_reason(change.reason)


# ------------------------------------------------------------------------------------------------ dont_know

def test_dont_know_is_the_questions_fallback_now_under_the_owner():
    stale, q = fact(), question()
    change = answer_updates(AnswerKind.DONT_KNOW, stale, q, NOW, MADRID, "digit")
    assert change.fields == fallback_fields(stale, q, NOW, "owner")
    assert (change.update, change.reason, change.evidence_at, change.history) == (None, None, None, None)
    assert change.fields["state"] is FactState.CURRENT
    assert change.fields["lifecycle_events"][-1]["actor"] == "owner"
    assert change.fields["lifecycle_events"][-1]["reason"] == answer_reason(AnswerKind.DONT_KNOW, "q1") == "did not remember q=q1"


def test_dont_know_on_an_archive_fallback_archives_it_under_the_owner():
    q = question(fallback="archive", noise=True)
    change = answer_updates(AnswerKind.DONT_KNOW, fact(), q, NOW, MADRID, "button")
    assert change.fields["state"] is FactState.ARCHIVED
    assert change.fields["lifecycle_events"][-1]["actor"] == "owner"


def test_dont_know_on_a_fact_that_is_closed_is_refused_by_the_fallback_rule():
    with pytest.raises(ValueError):
        answer_updates(AnswerKind.DONT_KNOW, fact(state=FactState.ARCHIVED), question(), NOW, MADRID, "digit")


# ------------------------------------------------------------------------------------------------ every kind

@pytest.mark.parametrize("kind", [AnswerKind.STILL_TRUE, AnswerKind.ENDED, AnswerKind.NEVER])
def test_an_update_carries_the_moment_of_the_answer_and_no_fallback_fields(kind):
    change = answer_updates(kind, fact(), question(), NOW, MADRID, "digit")
    assert change.evidence_at == NOW
    assert change.fields is None
    # sets nothing but its state and expiry (the old dict's keys were a subset of those two)
    assert change.update == ParsedFactUpdate(state=change.update.state, expiry=change.update.expiry)


def fitting_question(kind: AnswerKind):
    """P2 revised (C7): an answer is applied only to a question it is an option of; the first such kind, with its texts."""
    qkind = next(k for k, options in OPTIONS.items() if kind in options)
    if qkind == "still_true":
        return question()
    return question(question=qkind, fallback="history", texts=TEXTS[qkind])


@pytest.mark.parametrize("kind", list(AnswerKind))
def test_an_answer_changes_neither_the_fact_nor_the_question(kind):
    stale, q = fact(), fitting_question(kind)
    before = (stale.model_dump(), q.model_dump())
    answer_updates(kind, stale, q, NOW, MADRID, "digit")
    assert (stale.model_dump(), q.model_dump()) == before


# ------------------------------------------------------------------------------------------------ AnswerChange

def test_an_update_change_needs_its_updates_its_reason_and_the_moment():
    ok = AnswerChange(update=ARCHIVE, reason="r", evidence_at=NOW)
    assert ok.history is None
    for missing in ("update", "reason", "evidence_at"):
        args = {"update": ARCHIVE, "reason": "r", "evidence_at": NOW}
        args.pop(missing)
        with pytest.raises(ValueError):
            AnswerChange(**args)
    with pytest.raises(ValueError):
        AnswerChange(update=ARCHIVE, reason="", evidence_at=NOW)
    with pytest.raises(ValueError):
        AnswerChange()


def test_an_in_place_change_carries_nothing_else():
    assert AnswerChange(fields={"state": FactState.CURRENT}).fields == {"state": FactState.CURRENT}
    for extra in ({"update": ARCHIVE}, {"reason": "r"}, {"evidence_at": NOW}, {"history": "h"}):
        with pytest.raises(ValueError):
            AnswerChange(fields={"state": FactState.CURRENT}, **extra)


# ------------------------------------------------------------------------------------------------ the month names (L1)

def test_the_month_names_are_english_whatever_the_process_locale(monkeypatch):
    import locale
    monkeypatch.setattr(locale, "setlocale", lambda *a, **k: "uk_UA.UTF-8")  # nothing the text reads may go through it
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for month, name in enumerate(names, start=1):
        at = datetime(2026, month, 15, 12, 0, tzinfo=timezone.utc)
        assert ended_text("x", at, "UTC") == f"No longer true (the owner said so in {name} 2026): x"


# ------------------------------------------------------------------------------------------------ what a yes keeps (M1)

def test_still_true_on_a_permanent_fact_keeps_it_permanent():
    permanent = fact(state=FactState.CURRENT, stale_since=None, expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None)
    assert answer_updates(AnswerKind.STILL_TRUE, permanent, question(), NOW, MADRID, "digit").update == confirm(
        Expiry(ExpiryKind.NONE)
    )


@pytest.mark.parametrize("pinned", [
    {"domain": FactDomain.AGENT_DIRECTIVE}, {"type": FactType.PRINCIPLE}, {"tags": ["Mindset"]},
])
def test_still_true_on_a_pinned_fact_is_none_even_with_a_stale_horizon(pinned):
    change = answer_updates(AnswerKind.STILL_TRUE, fact(**pinned), question(), NOW, MADRID, "digit")
    assert change.update.expiry == Expiry(ExpiryKind.NONE)


def test_still_true_on_a_date_that_is_still_ahead_keeps_that_date():
    ahead = fact(state=FactState.CURRENT, expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=ENDS_28_OCT)
    expiry = answer_updates(AnswerKind.STILL_TRUE, ahead, question(), NOW, MADRID, "digit").update.expiry
    assert expiry == Expiry(ExpiryKind.DATE, on=date(2026, 10, 28))  # the last day of the plan, in the owner's zone


def test_the_date_is_read_in_the_owners_zone():
    ahead = fact(state=FactState.CURRENT, expiry_kind=ExpiryKind.DATE, ttl_days=None,
                 expires_at=datetime(2026, 10, 29, 23, 30, tzinfo=timezone.utc))  # 00:30 on 30 Oct in Europe/Madrid
    assert answer_updates(AnswerKind.STILL_TRUE, ahead, question(), NOW, MADRID, "digit").update.expiry.on == date(2026, 10, 30)
    assert answer_updates(AnswerKind.STILL_TRUE, ahead, question(), NOW, "UTC", "digit").update.expiry.on == date(2026, 10, 29)


@pytest.mark.parametrize("make", [
    lambda: fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=NOW),
    lambda: fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=NOW - DAY),
    lambda: fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=None),
    lambda: fact(),
    lambda: fact(expiry_kind=None, ttl_days=None, expires_at=None),
], ids=["date due now", "date past", "date without a moment", "due horizon", "legacy unclassified"])
def test_still_true_on_anything_else_gets_the_doubled_window(make):
    stale = make()
    days = answer_updates(AnswerKind.STILL_TRUE, stale, question(), NOW, MADRID, "digit").update.expiry
    assert days == Expiry(ExpiryKind.HORIZON, days=fallback_window_days(stale))


# ------------------------------------------------------------------------------------------------ the write path accepts them (L9)
# Reviewer edit (facts S4, T3c): the answer is the typed update the write path takes, so there is no parse left; the
# same values are asserted on it directly.

@pytest.mark.parametrize("make, expected", [
    (fact, Expiry(ExpiryKind.HORIZON, days=28)),
    (lambda: fact(expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None), Expiry(ExpiryKind.NONE)),
    (lambda: fact(domain=FactDomain.AGENT_DIRECTIVE), Expiry(ExpiryKind.NONE)),
    (lambda: fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=ENDS_28_OCT),
     Expiry(ExpiryKind.DATE, on=date(2026, 10, 28))),
])
def test_what_still_true_sends_parses_as_the_write_path_reads_it(make, expected):
    parsed = answer_updates(AnswerKind.STILL_TRUE, make(), question(), NOW, MADRID, "digit").update
    assert (parsed.state, parsed.expiry) == (FactState.CURRENT, expected)


@pytest.mark.parametrize("kind, state", [(AnswerKind.ENDED, FactState.ARCHIVED), (AnswerKind.NEVER, FactState.INVALIDATED)])
def test_what_ended_and_never_send_parse_as_a_closing_state_with_nothing_else(kind, state):
    parsed = answer_updates(kind, fact(), question(), NOW, MADRID, "digit").update
    assert (parsed.state, parsed.expiry, parsed.content, parsed.notes) == (state, None, None, ())
