"""The rules of a timed-out question and of a fact kept twice (Facts S4 · T8c, plan §3.2 steps 6-7, §3.8, S4-D19, S4-D20):
whether the asked document is still the one asked about, the neighbour check, a fact's neighbours, the kept-twice write."""
from datetime import timedelta

import pytest

from src.domain.entities import FactState
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reasons import is_revive_reason, kept_twice_reason
from src.domain.fact_review_rules import (
    KEPT_TWICE_ACTOR,
    asked_fact_unchanged,
    kept_twice_fields,
    queued_fields,
    revives_in_30_days,
    timeout_candidate,
)
from src.domain.fact_review_task import MAX_NEIGHBOURS, neighbours_of
from tests.support.review_rule_builders import NOW, fact, question

ASKED_AT = NOW - timedelta(days=15)


def asked(**over):
    return question(QuestionStatus.UNANSWERED, fact_id="f1", asked_at=ASKED_AT, **over)


def subject():
    return fact("f1", state=FactState.CURRENT, stale_since=None)


def neighbour(fact_id: str, created_days_ago: float, **over):
    return fact(fact_id, state=FactState.CURRENT, stale_since=None, created_at=NOW - timedelta(days=created_days_ago),
                **over)


# ------------------------------------------------------------------------------------------------ asked_fact_unchanged

def test_the_asked_document_itself_is_unchanged():
    assert asked_fact_unchanged(asked(), subject()) is True


@pytest.mark.parametrize("found", [None, fact("f2", lineage_id="L-f1", version=2, state=FactState.CURRENT)],
                         ids=["gone_or_closed", "rewritten"])
def test_a_fact_gone_closed_or_rewritten_is_no_longer_the_one_asked_about(found):
    assert asked_fact_unchanged(asked(), found) is False


# ------------------------------------------------------------------------------------------------ neighbours_of

def test_the_neighbours_are_the_first_five_outside_the_facts_own_lineage():
    own_version = fact("f1-v2", lineage_id="L-f1", state=FactState.CURRENT)
    others = [neighbour(f"n{n}", 100) for n in range(7)]

    found = neighbours_of(subject(), [subject(), own_version, *others])

    assert [f.id for f in found] == ["n0", "n1", "n2", "n3", "n4"]
    assert MAX_NEIGHBOURS == 5


# ------------------------------------------------------------------------------------------------ timeout_candidate

def test_a_neighbour_created_after_the_question_was_asked_makes_the_fact_a_candidate():
    assert timeout_candidate(subject(), asked(), [subject(), neighbour("old", 100), neighbour("new", 3)]) is True


@pytest.mark.parametrize("created", [ASKED_AT, ASKED_AT - timedelta(seconds=1)], ids=["at_asked_at", "before"])
def test_a_neighbour_not_newer_than_the_question_makes_no_candidate(created):
    old = fact("old", state=FactState.CURRENT, created_at=created)

    assert timeout_candidate(subject(), asked(), [subject(), old]) is False


def test_a_newer_document_of_the_facts_own_lineage_is_no_neighbour():
    own = fact("f1-v2", lineage_id="L-f1", state=FactState.CURRENT, created_at=NOW - timedelta(days=1))

    assert timeout_candidate(subject(), asked(), [subject(), own]) is False


def test_a_newer_fact_beyond_the_five_nearest_is_not_looked_at():
    nearest = [subject(), *[neighbour(f"n{n}", 100) for n in range(5)], neighbour("new", 1)]

    assert timeout_candidate(subject(), asked(), nearest) is False


def test_no_neighbours_make_no_candidate():
    assert timeout_candidate(subject(), asked(), []) is False


def test_a_question_never_asked_has_no_candidate():
    queued = question(QuestionStatus.QUEUED, fact_id="f1")

    assert timeout_candidate(subject(), queued, [neighbour("new", 1)]) is False


# ------------------------------------------------------------------------------------------------ kept_twice_fields

def test_a_kept_twice_fact_is_made_current_under_the_fallback_actor_and_is_no_revive():
    stale = fact("f1")

    fields = kept_twice_fields(stale, "qk", NOW)

    assert fields["state"] == "current" and fields["stale_since"] is None and fields["last_updated"] == NOW
    assert "expires_at" not in fields and "ttl_days" not in fields and "expiry_kind" not in fields   # expiry as stored
    event = fields["lifecycle_events"][-1]
    assert event == {"at": NOW, "from": "stale", "to": "current", "actor": KEPT_TWICE_ACTOR,
                     "reason": kept_twice_reason("qk")}
    assert KEPT_TWICE_ACTOR == "review_fallback"
    assert not is_revive_reason(event["reason"]) and revives_in_30_days(fields["lifecycle_events"], NOW) == 0


def test_a_kept_twice_write_differs_from_an_asks_only_in_its_trail_entry():
    stale = fact("f1")

    kept, ask = kept_twice_fields(stale, "q", NOW), queued_fields(stale, "q", NOW)

    assert {k: v for k, v in kept.items() if k != "lifecycle_events"} == \
        {k: v for k, v in ask.items() if k != "lifecycle_events"}
    assert (ask["lifecycle_events"][-1]["actor"], kept["lifecycle_events"][-1]["actor"]) == ("review", "review_fallback")


@pytest.mark.parametrize("state", [FactState.CURRENT, FactState.ARCHIVED])
def test_only_a_stale_fact_is_queued_as_kept_twice(state):
    with pytest.raises(ValueError):
        kept_twice_fields(fact("f1", state=state), "qk", NOW)
