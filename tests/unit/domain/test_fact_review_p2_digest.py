"""P2-3: the weekly digest's two new blocks (what silence wrote as history, the invoices waiting for a payment answer),
the Cabinet line, and which questions each block reads."""
from datetime import timedelta

import pytest

from src.domain.entities import FactState
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_digest import DIGEST_LIMIT, digest_histories, digest_invoices, digest_message, history_line
from src.domain.fact_review_language import CANONICAL_LABELS
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.ui_messages import UIMessage
from src.locales import uk
from tests.support.clock_instants import BERLIN, OCT_END
from tests.support.fact_kit import make_fact
from tests.support.review_rule_builders import NOW, question
from tests.unit.domain.review_p2_texts import DIGEST_TEXTS, HAPPENED_TEXTS

S = QuestionStatus
LABELS = dict(CANONICAL_LABELS)
URL = "https://cabinet.example/cabinet?tab=memory"
HISTORY = HAPPENED_TEXTS["unknown"]
INVOICE = "An invoice for the phone bill (45 EUR), due Nov 3, 2026, was received; payment not confirmed."
TENNIS = "The user goes to a tennis lesson on Sundays."


def archived(text=TENNIS):
    event = {"at": NOW, "from": "current", "to": "archived", "actor": "review", "reason": "code-built"}
    return make_fact(text, state=FactState.ARCHIVED, lifecycle_events=[event])


def s4_text(facts):
    """The S4 digest, as `digest_message` built it before P2."""
    lines = [f"• «{f.text}» — {LABELS[UIMessage.MEMORY_DIGEST_ARCHIVED.value]}" for f in facts]
    return "\n".join([LABELS["memory_digest_header"], *lines, "", LABELS["memory_digest_footer"]])


# ------------------------------------------------------------------------------------------------ the message

def test_removals_only_are_the_s4_text_byte_for_byte():
    facts = [archived(), archived("The user had a gym membership.")]
    assert digest_message(LABELS, facts) == s4_text(facts)
    assert digest_message(LABELS, facts, cabinet_url=URL) == s4_text(facts)  # no invoices, no URL


def test_all_three_sections_joined_by_one_blank_line():
    text = digest_message(LABELS, [archived()], histories=[HISTORY], invoices=[INVOICE], cabinet_url=URL)
    assert text == "\n\n".join([
        s4_text([archived()]),
        f"Recorded as history without your answer:\n• «{HISTORY}»",
        f"Invoices with no payment confirmation:\n• «{INVOICE}»\nMark the paid ones in the Cabinet:\n{URL}",
    ])


def test_a_digest_with_only_histories_is_built():
    assert digest_message(LABELS, [], histories=[HISTORY, "The user had the car serviced."]) == (
        f"Recorded as history without your answer:\n• «{HISTORY}»\n• «The user had the car serviced.»"
    )


def test_a_digest_with_only_invoices_ends_with_the_cabinet_line_and_the_url():
    text = digest_message(LABELS, [], invoices=[INVOICE], cabinet_url=URL)
    assert text.splitlines()[-2:] == ["Mark the paid ones in the Cabinet:", URL]


def test_without_a_url_there_is_no_cabinet_line():
    assert digest_message(LABELS, [], invoices=[INVOICE]) == f"Invoices with no payment confirmation:\n• «{INVOICE}»"


def test_nothing_to_list_is_no_digest():
    with pytest.raises(ValueError):
        digest_message(LABELS, [], histories=[], invoices=[], cabinet_url=URL)


def test_a_removal_that_is_not_set_aside_is_still_refused():
    with pytest.raises(ValueError):
        digest_message(LABELS, [make_fact(TENNIS, state=FactState.CURRENT)], histories=[HISTORY])


def test_every_line_is_shown_through_its_translation_and_english_without_one():
    labels = {key: uk.UI_STRINGS[key] for key in LABELS}
    history_uk = "Користувач планував піти на концерт 5 грудня 2026 року; чи відбувся він, невідомо."
    translations = {quote_digest(HISTORY): QuoteTranslation("uk", quote_digest(HISTORY), history_uk)}
    text = digest_message(labels, [], translations, "uk", histories=[HISTORY], invoices=[INVOICE], cabinet_url=URL)
    assert text == "\n".join([
        uk.UI_STRINGS["memory_digest_history_header"], f"• «{history_uk}»",
        "",
        uk.UI_STRINGS["memory_digest_invoices_header"], f"• «{INVOICE}»",
        uk.UI_STRINGS["memory_digest_invoices_cabinet"], URL,
    ])


def test_a_line_is_one_line():
    assert "• «The user went  to the\nconcert.»" not in digest_message(LABELS, [], histories=["The user went  to the\nconcert."])
    assert digest_message(LABELS, [], histories=["The user went  to the\nconcert."]).endswith("• «The user went to the concert.»")


# ------------------------------------------------------------------------------------------------ which questions

def timed_out(question_id, closed_at, **over):
    fields = dict(question="happened", fallback="history", texts=HAPPENED_TEXTS)
    fields.update(over)
    asked = question(S.ASKED, question_id=question_id, asked_at=closed_at - timedelta(days=14), **fields)
    return asked.closed(ClosedBy.TIMEOUT, closed_at)


def test_the_histories_are_the_timeouts_of_history_questions_since_newest_first():
    since = NOW - timedelta(days=7)
    older = timed_out("q-old", NOW - timedelta(days=2))
    newer = timed_out("q-new", NOW - timedelta(days=1))
    before = timed_out("q-before", since)
    revived = question(S.ASKED, question_id="q-rev").closed(ClosedBy.TIMEOUT, NOW - timedelta(days=1))
    owner = question(S.ASKED, question_id="q-own", question="happened", fallback="history", texts=HAPPENED_TEXTS).closed(
        ClosedBy.OWNER, NOW - timedelta(days=1), answer=AnswerKind.DONT_KNOW, source="digit",
    )
    listed = digest_histories([older, before, revived, owner, newer], since)
    assert [q.question_id for q in listed] == ["q-new", "q-old"]


def test_the_histories_are_at_most_the_digest_limit():
    many = [timed_out(f"q{i:03}", NOW - timedelta(minutes=i)) for i in range(DIGEST_LIMIT + 3)]
    listed = digest_histories(many, NOW - timedelta(days=7))
    assert len(listed) == DIGEST_LIMIT and listed[0].question_id == "q000"


def digest_q(question_id, queued_at):
    return question(
        question_id=question_id, question="paid", fallback="history", texts=DIGEST_TEXTS, delivery="digest",
        queued_at=queued_at, queue_key=queued_at,
    )


def test_the_invoices_are_the_open_digest_questions_oldest_first():
    first = digest_q("b", NOW - timedelta(days=3))
    same_day = digest_q("a", NOW - timedelta(days=3))
    later = digest_q("c", NOW - timedelta(days=1))
    chat = question(question_id="z")
    answered = digest_q("d", NOW - timedelta(days=9)).closed(ClosedBy.RESOLVED_ELSEWHERE, NOW)
    assert [q.question_id for q in digest_invoices([later, chat, first, answered, same_day])] == ["a", "b", "c"]


def test_the_invoices_are_at_most_the_digest_limit():
    many = [digest_q(f"q{i:03}", NOW - timedelta(minutes=i)) for i in range(DIGEST_LIMIT + 2)]
    assert len(digest_invoices(many)) == DIGEST_LIMIT


# ------------------------------------------------------------------------------------------------ a history line

def test_a_review_questions_history_line_is_its_stored_unknown_text():
    assert history_line(timed_out("q1", NOW), BERLIN) == HAPPENED_TEXTS["unknown"]


def test_a_kept_twice_history_line_is_built_in_english_in_the_month_it_closed():
    kept = timed_out("q1", OCT_END.utc, texts={}, origin="kept_twice")
    assert history_line(kept, BERLIN) == (
        f"Planned; whether it took place is unknown (no answer by Nov 2026): {kept.quoted_text}"
    )


def test_an_open_question_has_no_history_line():
    with pytest.raises(ValueError):
        history_line(question(question="happened", fallback="history", texts=HAPPENED_TEXTS), BERLIN)
