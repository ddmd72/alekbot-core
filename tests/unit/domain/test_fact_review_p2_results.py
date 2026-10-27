"""P2-2, P2-3: what a pass returns (its invoices, its asks' kind, texts and folds), the translations of the texts it
wrote, what follows the pass (folds' offers, the history count), the new counters and the Cabinet's queue entry."""
from datetime import date, timedelta

import pytest

from src.domain.fact_lifecycle_day import REVIEW_FIGURES, CounterField, FactLifecycleDay
from src.domain.fact_lifecycle_review_line import LINE_COUNTERS, QuestionStanding, review_segments
from src.domain.fact_review_after_pass import after_pass
from src.domain.fact_review_ask import AskRequest
from src.domain.fact_review_invoice import InvoiceRequest
from src.domain.fact_review_outcome import ReviewDecision
from src.domain.fact_review_overview import queue_entries
from src.domain.fact_review_quote_translation import (
    HISTORY_TEXT_KEY, QuoteTranslation, quote_digest, report_translations, written_text_key,
)
from src.domain.fact_review_result import FactReviewResult, decode_review_result, encode_review_result
from tests.support.review_rule_builders import NOW, fact, question
from tests.unit.domain.review_p2_texts import DIGEST_TEXTS, HAPPENED_TEXTS

PLAN = "The user plans to go to the concert on Dec 5, 2026."
HISTORY = "The user went to the concert on Dec 5, 2026."
HISTORY_UK = "Користувач пішов на концерт 5 грудня 2026 року."
INVOICE = InvoiceRequest("f2", "f2-v2", DIGEST_TEXTS["paid"], DIGEST_TEXTS["not_paid"])
ASK = AskRequest("f1", "history", question="happened", texts=dict(HAPPENED_TEXTS), folds=("f3", "f4"))


# ------------------------------------------------------------------------------------------------ translations

def test_the_key_of_a_written_text():
    assert HISTORY_TEXT_KEY == "history"
    assert written_text_key("f1", HISTORY_TEXT_KEY) == "f1:history"
    assert written_text_key("f1", "not_happened") == "f1:not_happened"


def test_a_written_text_entry_is_kept_under_its_englishs_digest():
    sources = {"f1": PLAN, written_text_key("f1", HISTORY_TEXT_KEY): HISTORY}
    kept, refusals = report_translations({"f1:history": f" {HISTORY_UK} "}, "uk", sources)
    assert kept == (QuoteTranslation("uk", quote_digest(HISTORY), HISTORY_UK),) and refusals == []


def test_an_unknown_key_and_a_changed_number_are_refused_naming_only_the_key():
    sources = {"f1": PLAN, "f1:history": HISTORY}
    raw = {"f1:history": "Користувач пішов на концерт 6 грудня 2026 року.", "f1:paid": "Оплачено.", "f9": "x"}
    kept, refusals = report_translations(raw, "uk", sources)
    assert kept == ()
    assert sorted(refusals) == sorted([
        "f1:paid: names no listed fact or text this pass wrote",
        "f9: names no listed fact or text this pass wrote",
        "f1:history: the numbers differ from the source",
    ])


# ------------------------------------------------------------------------------------------------ the result's plain form

def test_the_result_round_trips_with_invoices_and_the_new_ask_fields():
    result = FactReviewResult(
        outcomes=(ReviewDecision("f2", "rewritten", "f2-v2", "date_passed", as_history=True),),
        asks=(ASK,), kept=("f5",), invoices=(INVOICE,),
    )
    encoded = encode_review_result(result)
    assert encoded["invoices"] == [{"fact_id": "f2", "written_id": "f2-v2", "paid": DIGEST_TEXTS["paid"],
                                    "not_paid": DIGEST_TEXTS["not_paid"]}]
    assert encoded["asks"][0]["folds"] == ["f3", "f4"] and encoded["asks"][0]["texts"] == HAPPENED_TEXTS
    assert encoded["outcomes"][0]["as_history"] is True
    assert decode_review_result(encoded) == result


def test_a_result_from_before_p2_reads_with_no_invoices_and_s4_asks():
    old = {
        "outcomes": [{"fact_id": "f1", "outcome": "revived", "written_id": "f1", "basis_kind": "evidence"}],
        "asks": [{"fact_id": "f2", "fallback": "revive", "fallback_date": None, "noise": False}],
        "kept": [],
    }
    decoded = decode_review_result(old)
    assert decoded.invoices == ()
    assert decoded.asks == (AskRequest("f2", "revive"),)
    assert decoded.outcomes[0].as_history is False


def test_a_malformed_invoice_is_an_invalid_result():
    with pytest.raises(ValueError):
        decode_review_result({"invoices": [{"fact_id": "f2"}]})


# ------------------------------------------------------------------------------------------------ after the pass

def test_each_history_rewrite_is_counted_as_to_history_besides_rewritten():
    batch = [fact("f1"), fact("f2"), fact("f3")]
    result = FactReviewResult(outcomes=(
        ReviewDecision("f1", "rewritten", "f1-v2", "date_passed", as_history=True),
        ReviewDecision("f2", "rewritten", "f2-v2", "evidence"),
        ReviewDecision("f3", "rewritten", "f3-v2", "reply", as_history=True),
    ))
    counts = after_pass(batch, [], result, NOW).counts
    assert counts[CounterField.REWRITTEN] == 3 and counts[CounterField.TO_HISTORY] == 2


def test_no_history_rewrite_counts_no_to_history():
    result = FactReviewResult(outcomes=(ReviewDecision("f1", "revived", "f1", "evidence"),))
    assert CounterField.TO_HISTORY not in after_pass([fact("f1")], [], result, NOW).counts


def test_the_folds_lose_their_offers_only_with_their_asks_landed_queue_write():
    batch = [fact(f) for f in ("f1", "f2", "f3", "f4", "f6")]
    other = AskRequest("f6", "revive", folds=())
    result = FactReviewResult(
        outcomes=(ReviewDecision("f2", "rewritten", "f2-v2", "date_passed", as_history=True),), asks=(ASK, other),
    )
    done = after_pass(batch, [], result, NOW)
    assert done.offers_removed(["f1"]) == ("f2", "f1", "f3", "f4")
    assert done.offers_removed(["f6"]) == ("f2", "f6")
    assert done.offers_removed([]) == ("f2",)


# ------------------------------------------------------------------------------------------------ the counters

def test_the_new_counters_are_stored_and_the_line_counters_lead_the_review_figures():
    for name in ("to_history", "folded", "fallbacks_not_applied"):
        assert CounterField(name).value == name
        assert getattr(FactLifecycleDay(account_id="acc", day=date(2026, 11, 1)), name) == 0
    assert REVIEW_FIGURES[:len(LINE_COUNTERS)] == LINE_COUNTERS and len(LINE_COUNTERS) == 20
    assert len(REVIEW_FIGURES) == 25
    assert LINE_COUNTERS.index(CounterField.TO_HISTORY) == LINE_COUNTERS.index(CounterField.QUEUED) + 1
    assert LINE_COUNTERS.index(CounterField.FOLDED) == LINE_COUNTERS.index(CounterField.QUEUED) + 2
    assert LINE_COUNTERS.index(CounterField.FALLBACKS_NOT_APPLIED) == LINE_COUNTERS.index(CounterField.REVIEW_REFUSALS) + 1


def test_the_new_buckets_print_in_their_segments():
    row = FactLifecycleDay(account_id="acc", day=date(2026, 11, 1), reviewed=4, rewritten=2, queued=1, to_history=2,
                           folded=2, fallbacks_not_applied=1, review_enqueued=1)
    assert review_segments(row, 0, QuestionStanding()) == (
        " | review: reviewed 4; counted on their own: revived 0, rewritten 2, archived 0, invalidated 0, superseded 0,"
        " kept 0, queued 1, to history 2, folded 2"
        " | review failed 0, refusals 0, fallbacks not applied 1"
        " | enqueued 1"
    )


def test_a_lone_failed_fallback_earns_the_failure_segment():
    row = FactLifecycleDay(account_id="acc", day=date(2026, 11, 1), fallbacks_not_applied=1)
    assert review_segments(row, 0, QuestionStanding()) == " | review failed 0, refusals 0, fallbacks not applied 1 | enqueued 0"


# ------------------------------------------------------------------------------------------------ the Cabinet queue

def test_a_queue_entry_carries_the_questions_kind_and_delivery():
    chat = question(question_id="qa", question="happened", fallback="history", texts=HAPPENED_TEXTS,
                    queued_at=NOW - timedelta(days=2))
    digest = question(question_id="qb", fact_id="f2", question="paid", fallback="history", texts=DIGEST_TEXTS,
                      delivery="digest", queue_key=NOW - timedelta(days=1), queued_at=NOW - timedelta(days=1))
    entries = {e.question_id: e for e in queue_entries(None, [chat, digest], {}, "acc")}
    assert (entries["qa"].question, entries["qa"].delivery) == ("happened", "chat")
    assert (entries["qb"].question, entries["qb"].delivery) == ("paid", "digest")
