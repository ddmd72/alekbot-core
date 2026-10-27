"""Facts S4 · T8d2: the weekly digest's rules (plan §3.11): the window, which facts, the message, the week's day, what in
a pass or an answer sets a fact aside, and the texts of the digest in every language the UI has."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import FactState
from src.domain.fact_lifecycle_day import CounterField
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import answer_closes_fact
from src.domain.fact_review_digest import (
    DIGEST_LIMIT, FIRST_DIGEST_WINDOW, LISTED_ACTORS, digest_day, digest_facts, digest_message, digest_since, last_actor,
    pass_set_aside,
)
from src.domain.fact_review_language import CANONICAL_LABELS, LABEL_KEYS, check_labels
from src.domain.fact_review_rules import iso_week_of
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk
from tests.support.fact_kit import make_fact
from tests.support.review_rule_builders import NOW, question, state

ARCHIVED = UIMessage.MEMORY_DIGEST_ARCHIVED.value
INVALIDATED = UIMessage.MEMORY_DIGEST_INVALIDATED.value
LABELS = {**CANONICAL_LABELS}


def closed(text: str, actor: str = "review", state_: FactState = FactState.ARCHIVED):
    event = {"at": NOW, "from": "current", "to": state_.value, "actor": actor, "reason": "code-built"}
    return make_fact(text, state=state_, lifecycle_events=[event])


# ------------------------------------------------------------------------------------------------ the window

def test_the_window_starts_at_the_last_digest():
    last = NOW - timedelta(days=8)
    assert digest_since(state(last_digest_at=last), NOW) == last


def test_the_first_digest_looks_back_thirty_days():
    assert FIRST_DIGEST_WINDOW == timedelta(days=30)
    assert digest_since(state(), NOW) == NOW - timedelta(days=30)


def test_the_day_is_the_owners_local_day():
    sunday_late = datetime(2026, 7, 12, 23, 30, tzinfo=timezone.utc)
    assert digest_day(sunday_late, "Europe/Lisbon") == date(2026, 7, 13)
    assert digest_day(sunday_late, "UTC") == date(2026, 7, 12)
    assert iso_week_of(digest_day(sunday_late, "Europe/Lisbon")) == "2026-W29"
    assert iso_week_of(digest_day(sunday_late, "UTC")) == "2026-W28"


# ------------------------------------------------------------------------------------------------ which facts

def test_the_listed_actors_are_the_review_the_timeout_and_the_owner():
    assert LISTED_ACTORS == {"review", "timeout", "owner"}


def test_the_last_actor_is_the_last_trail_entrys():
    fact = make_fact("Alex keeps a weight log", lifecycle_events=[
        {"at": NOW, "from": None, "to": "current", "actor": "stage1", "reason": "x"},
        {"at": NOW, "from": "current", "to": "archived", "actor": "owner", "reason": "y"},
    ])
    assert last_actor(fact) == "owner"
    assert last_actor(make_fact("Alex walks to Main Square")) is None


@pytest.mark.parametrize("actor, listed", [
    ("review", True), ("timeout", True), ("owner", True), ("stage2b", False), ("cluster", False), ("janitor", False),
])
def test_a_fact_is_listed_by_its_last_actor(actor, listed):
    assert (digest_facts([closed("Alex keeps a weight log", actor)]) != []) is listed


@pytest.mark.parametrize("state_, listed", [
    (FactState.ARCHIVED, True), (FactState.INVALIDATED, True), (FactState.CURRENT, False), (FactState.STALE, False),
    (FactState.SUPERSEDED, False),
])
def test_only_removals_are_listed(state_, listed):
    assert (digest_facts([closed("Alex keeps a weight log", "review", state_)]) != []) is listed


def test_a_fact_without_a_trail_is_not_listed():
    assert digest_facts([make_fact("Alex keeps a weight log", state=FactState.ARCHIVED)]) == []


def test_the_order_given_is_kept_and_the_list_is_capped():
    facts = [closed(f"neutral fact {n:02d}") for n in range(DIGEST_LIMIT + 5)]
    assert digest_facts(facts) == facts[:DIGEST_LIMIT] and DIGEST_LIMIT == 50


# ------------------------------------------------------------------------------------------------ the message

def test_the_message_is_the_header_a_line_per_fact_a_blank_line_and_the_footer():
    facts = [closed("Alex keeps a weight log"), closed("Alex orders a cookbook", "owner", FactState.INVALIDATED)]

    assert digest_message(LABELS, facts) == "\n".join([
        "Facts I put aside this week:",
        "• «Alex keeps a weight log» — no longer true",
        "• «Alex orders a cookbook» — never was true",
        "",
        "To bring one back, tell me in chat that it is still true.",
    ])


def test_a_fact_is_quoted_on_one_line_as_stored():
    text = "Alex  plans a weekend\nin Coimbra, «Mar 6–8»  <b>"
    assert digest_message(LABELS, [closed(text)]).split("\n")[1] == "• «Alex plans a weekend in Coimbra, «Mar 6–8» <b>» — no longer true"


def test_the_lines_use_the_given_language():
    labels = {**LABELS, ARCHIVED: "já não é verdade", "memory_digest_footer": "rodapé", "memory_digest_header": "cabeça"}
    assert digest_message(labels, [closed("Alex keeps a weight log")]).split("\n") == [
        "cabeça", "• «Alex keeps a weight log» — já não é verdade", "", "rodapé"]


def test_no_facts_or_a_fact_that_is_no_removal_is_refused():
    with pytest.raises(ValueError):
        digest_message(LABELS, [])
    with pytest.raises(ValueError):
        digest_message(LABELS, [make_fact("Alex keeps a weight log")])


# ------------------------------------------------------------------------------------------------ what sets a fact aside

@pytest.mark.parametrize("counts, aside", [
    ({}, False),
    ({CounterField.REVIEWED: 3, CounterField.QUEUED: 1, CounterField.TIMED_OUT: 2}, False),
    ({CounterField.ARCHIVED_BY_REVIEW: 1}, True),
    ({CounterField.INVALIDATED: 2}, True),
    ({CounterField.ARCHIVED_BY_REVIEW: 0, CounterField.INVALIDATED: 0}, False),
])
def test_a_pass_sets_a_fact_aside_when_it_counts_an_archive_or_an_invalidation(counts, aside):
    assert pass_set_aside(counts) is aside


@pytest.mark.parametrize("kind, fallback, closes", [
    (AnswerKind.ENDED, "revive", True), (AnswerKind.NEVER, "revive", True), (AnswerKind.STILL_TRUE, "archive", False),
    (AnswerKind.DONT_KNOW, "archive", True), (AnswerKind.DONT_KNOW, "revive", False),
])
def test_an_answer_sets_the_fact_aside_by_its_kind_and_the_questions_fallback(kind, fallback, closes):
    assert answer_closes_fact(kind, question(fallback=fallback)) is closes


# ------------------------------------------------------------------------------------------------ the texts

def test_the_state_words_are_label_keys_with_canonical_english_texts():
    assert {ARCHIVED, INVALIDATED} <= LABEL_KEYS and len(LABEL_KEYS) == 28  # P2 revised (C11): 15 + 13
    assert (CANONICAL_LABELS[ARCHIVED], CANONICAL_LABELS[INVALIDATED]) == ("no longer true", "never was true")
    assert check_labels(dict(CANONICAL_LABELS)) is None


@pytest.mark.parametrize("module", [en, uk, fr, es])
def test_every_ui_language_has_the_digest_texts_and_they_pass_the_label_check(module):
    strings = {key: module_text for key in LABEL_KEYS if (module_text := _strings(module).get(key))}
    assert set(strings) == LABEL_KEYS
    assert check_labels(strings) is None


def test_the_english_ui_strings_are_the_canonical_texts():
    assert {key: _strings(en)[key] for key in LABEL_KEYS} == dict(CANONICAL_LABELS)


def _strings(module) -> dict:
    return next(v for k, v in vars(module).items() if isinstance(v, dict) and UIMessage.MEMORY_DIGEST_FOOTER.value in v)


def test_the_languages_the_test_covers_are_the_ui_languages():
    assert {LanguageCode.UK, LanguageCode.EN, LanguageCode.FR, LanguageCode.ES} == set(LanguageCode)
