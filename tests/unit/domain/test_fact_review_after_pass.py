"""The rules after a review pass and around it (Facts S4 · T8b, plan §3.2 steps 8, 10, 11; §3.4, §3.17): what the pass's
result decides (`after_pass`), the result's one plain-data form, the offers count, the queue write, the review close's
source, the prefetch's shape, and what a mirror-mode report changes in the state."""
from datetime import timedelta

import pytest

from src.domain.entities import FactState
from src.domain.fact_lifecycle_day import CounterField
from src.domain.fact_review_after_pass import after_pass
from src.domain.fact_review_ask import AskRequest
from src.domain.fact_review_language import CANONICAL_LABELS, labels_digest, language_to_store
from src.domain.fact_review_outcome import ReviewDecision
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reasons import is_revive_reason, queued_reason
from src.domain.fact_review_result import FactReviewResult, decode_review_result, encode_review_result
from src.domain.fact_review_rules import (
    fallback_revives, offered, queued_fields, review_close_source, unread_replies,
)
from src.domain.fact_review_state import FactReviewState
from src.domain.fact_review_task import (
    MAX_NEIGHBOURS, owner_message_texts, questions_about, review_item,
)
from src.domain.llm import Message, MessagePart
from tests.support.review_rule_builders import NOW, fact, question

PT = {key: f"texto {chr(97 + n)}" for n, key in enumerate(sorted(CANONICAL_LABELS))}


def decision(fact_id: str, outcome: str = "revived") -> ReviewDecision:
    return ReviewDecision(fact_id, outcome, fact_id, "evidence")  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ after_pass

def test_after_pass_decides_closes_fallbacks_asks_revives_and_counts():
    batch = [fact("f1"), fact("f2", state=FactState.CURRENT), fact("f3", state=FactState.CURRENT), fact("f4"), fact("f5")]
    replied = question(QuestionStatus.ASKED, question_id="q2", fact_id="f2", reply_text="still true",
                       reply_at=NOW - timedelta(hours=1))
    timed_out = question(QuestionStatus.UNANSWERED, question_id="q3", fact_id="f3", asked_at=NOW - timedelta(days=15))
    elsewhere = question(QuestionStatus.ASKED, question_id="q9", fact_id="f9")
    result = FactReviewResult(
        outcomes=(decision("f1"), decision("f2", "archived"), decision("f-outside")),
        asks=(AskRequest("f4", "revive"), AskRequest("f-outside", "revive")),
        kept=("f3", "f5"),
    )

    plan = after_pass(batch, [replied, timed_out, elsewhere], result, NOW)

    assert plan.decided == ("f1", "f2")
    assert [q.question_id for q in plan.closes] == ["q2"]
    assert [(f.id, q.question_id) for f, q in plan.fallbacks] == [("f3", "q3")]
    assert [(f.id, a.fact_id) for f, a in plan.asks] == [("f4", "f4")]
    assert plan.revived == ("f1",)
    assert dict(plan.counts) == {
        CounterField.REVIEWED: 5, CounterField.KEPT: 2, CounterField.REVIVED: 1, CounterField.ARCHIVED_BY_REVIEW: 1,
        CounterField.CLOSED_BY_REPLY: 1,
    }


def test_the_offers_entries_removed_are_the_decided_facts_and_the_queued_asks():
    batch = [fact("f1"), fact("f2"), fact("f3"), fact("f4")]
    result = FactReviewResult(outcomes=(decision("f1"),), asks=(AskRequest("f2", "revive"), AskRequest("f3", "revive")),
                              kept=("f4",))

    plan = after_pass(batch, [], result, NOW)

    assert plan.offers_removed(["f2"]) == ("f1", "f2")                 # f3's queue write did not land
    assert plan.offers_removed(["f4", "f2", "f2"]) == ("f1", "f2")     # only an ask of the pass is queued
    assert plan.offers_removed([]) == ("f1",)


def test_a_decided_or_asked_candidate_gets_no_fallback():
    batch = [fact("f1", state=FactState.CURRENT)]
    timed_out = question(QuestionStatus.UNANSWERED, fact_id="f1", asked_at=NOW - timedelta(days=15))

    decided = after_pass(batch, [timed_out], FactReviewResult(outcomes=(decision("f1", "invalidated"),)), NOW)
    asked = after_pass(batch, [timed_out], FactReviewResult(asks=(AskRequest("f1", "revive"),)), NOW)

    assert decided.fallbacks == () and [q.question_id for q in decided.closes] == ["q1"]
    assert asked.fallbacks == ()


def test_a_question_not_yet_timed_out_gets_no_fallback():
    batch = [fact("f1", state=FactState.CURRENT)]
    waiting = question(QuestionStatus.ASKED, fact_id="f1", asked_at=NOW - timedelta(days=13))

    assert after_pass(batch, [waiting], FactReviewResult(kept=("f1",)), NOW).fallbacks == ()


# ------------------------------------------------------------------------------------------------ the result's form

def test_the_result_round_trips_through_its_plain_form():
    result = FactReviewResult(
        outcomes=(decision("f1"),), asks=(AskRequest("f2", "archive", fallback_date=NOW.date(), noise=True),),
        kept=("f3",), message_language="pt", labels={"k": "v"},
    )

    encoded = encode_review_result(result)

    assert encoded == {
        # P2 revised (C8): a decision carries as_history, an ask its question, texts and folds, the result its invoices
        "outcomes": [{"fact_id": "f1", "outcome": "revived", "written_id": "f1", "basis_kind": "evidence",
                      "as_history": False}],
        "asks": [{"fact_id": "f2", "fallback": "archive", "fallback_date": NOW.date(), "noise": True,
                  "question": "still_true", "texts": {}, "folds": []}],
        "kept": ["f3"], "message_language": "pt", "labels": {"k": "v"}, "translations": [], "invoices": [],
    }
    assert decode_review_result(encoded) == result


def test_a_missing_result_or_part_is_empty():
    assert decode_review_result(None) == FactReviewResult()
    assert decode_review_result({}) == FactReviewResult()


@pytest.mark.parametrize("raw", [
    "not an object",
    {"outcomes": [{"fact_id": "f1"}]},
    {"outcomes": [{"fact_id": "f1", "outcome": "kept", "written_id": "f1", "basis_kind": None}]},
    {"asks": [{"fact_id": "f1", "fallback": "revive", "why": "x"}]},
    {"kept": [1]},
    {"labels": ["Sim"]},
    {"message_language": 7},
])
def test_a_result_not_in_the_plain_form_is_refused(raw):
    with pytest.raises(ValueError):
        decode_review_result(raw)


# ------------------------------------------------------------------------------------------------ selection, offers, queue

def test_offers_count_the_stale_batch_facts_only():
    batch = [fact("f1"), fact("f2", state=FactState.CURRENT), fact("f3")]

    assert offered({"f1": 1, "f9": 2}, batch) == {"f1": 2, "f3": 1, "f9": 2}


def test_unread_replies_are_in_queue_order_and_a_shown_one_is_not_among_them():
    later = question(QuestionStatus.ASKED, question_id="q2", fact_id="f2", reply_text="yes",
                     reply_at=NOW, queue_key=NOW - timedelta(days=1))
    earlier = question(QuestionStatus.UNANSWERED, question_id="q1", reply_text="no",
                       reply_at=NOW, queue_key=NOW - timedelta(days=5))
    shown = question(QuestionStatus.ASKED, question_id="q3", fact_id="f3", reply_text="maybe",
                     reply_at=NOW, offered_at=NOW)
    silent = question(QuestionStatus.ASKED, question_id="q4", fact_id="f4")

    assert [q.question_id for q in unread_replies([later, shown, earlier, silent])] == ["q1", "q2"]


def test_the_queue_write_makes_a_stale_fact_current_with_its_expiry_and_a_non_revive_entry():
    stale = fact("f1")

    fields = queued_fields(stale, "q7", NOW)

    assert fields["state"] is FactState.CURRENT and fields["stale_since"] is None and fields["valid_to"] is None
    assert not {"expires_at", "expiry_kind", "ttl_days"} & set(fields)
    event = fields["lifecycle_events"][-1]
    assert (event["at"], event["from"], event["to"], event["actor"]) == (NOW, "stale", "current", "review")
    assert event["reason"] == queued_reason("q7") and not is_revive_reason(event["reason"])


def test_only_a_stale_fact_is_queued():
    with pytest.raises(ValueError):
        queued_fields(fact("f1", state=FactState.CURRENT), "q7", NOW)


def test_a_review_close_on_the_owners_words_has_the_source_review():
    assert review_close_source(question(QuestionStatus.ASKED, reply_text="yes", reply_at=NOW)) == "review"
    assert review_close_source(question(QuestionStatus.ASKED)) is None


def test_only_a_revive_fallback_revives():
    assert fallback_revives(question(fallback="revive")) is True
    assert fallback_revives(question(fallback="archive")) is False


# ------------------------------------------------------------------------------------------------ the prefetch's shape

def test_a_review_item_has_the_earlier_versions_the_nearest_outside_its_lineage_and_no_vectors():
    v1 = fact("v1", lineage_id="L", version=1, state=FactState.SUPERSEDED, vector=[0.1])
    v2 = fact("v2", lineage_id="L", version=2, vector=[0.2])
    v3 = fact("v3", lineage_id="L", version=3, state=FactState.CURRENT)
    near = [fact(f"n{n}", state=FactState.CURRENT, vector=[0.3], tags_vector=[0.4]) for n in range(6)]

    item = review_item(v2, [v1, v2, v3], [v3, *near])

    assert item.fact.id == "v2" and [v.id for v in item.versions] == ["v1"]
    assert [n.id for n in item.neighbours] == [f"n{n}" for n in range(MAX_NEIGHBOURS)]
    for f in (item.fact, *item.versions, *item.neighbours):
        assert (f.vector, f.tags_vector, f.metadata_vector) == (None, None, None)


def test_the_questions_about_a_batch_are_its_open_ones():
    open_one = question(QuestionStatus.ASKED, question_id="q1", fact_id="f1")
    other = question(QuestionStatus.ASKED, question_id="q2", fact_id="f2")
    closed = question(QuestionStatus.CLOSED, question_id="q3", fact_id="f1")

    assert [q.question_id for q in questions_about([fact("f1")], [open_one, other, closed])] == ["q1"]


def test_the_owners_texts_are_his_parts_oldest_first():
    history = [
        Message(role="user", parts=[MessagePart(text="first"), MessagePart(text=None)]),
        Message(role="model", parts=[MessagePart(text="reply")]),
        Message(role="user", parts=[MessagePart(text="second")]),
    ]

    assert owner_message_texts(history) == ["first", "second"]


# ------------------------------------------------------------------------------------------------ the reported language

def state(**over) -> FactReviewState:
    return FactReviewState(account_id="acc", **over)


def test_a_language_without_ui_strings_stores_a_checked_label_set_for_todays_texts():
    assert language_to_store(state(), " PT ", PT) == ("pt", PT, labels_digest())


def test_a_refused_label_set_keeps_the_cached_one_when_the_language_changes():
    cached = state(message_language="uk", labels={"pt": PT}, label_digests={"pt": "old"})

    assert language_to_store(cached, "pt", {**PT, sorted(PT)[0]: "with 1 digit"}) == ("pt", PT, "old")


def test_a_ui_strings_language_stores_no_labels_of_the_model():
    assert language_to_store(state(), "uk", PT) == ("uk", {}, "")


@pytest.mark.parametrize("reported, labels", [(None, PT), ("Portuguese", PT), ("pt", None)])
def test_nothing_changes_without_a_language_or_with_the_same_one_and_no_usable_labels(reported, labels):
    assert language_to_store(state(message_language="pt"), reported, labels) is None
