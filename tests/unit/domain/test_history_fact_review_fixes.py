"""Facts S4 · T3a review fixes, in the domain.

- M1: a history archive refuses a fact rewritten since the owner was asked (`history_moved_refusal`, the rule
  `walked_to_unseen_text` the Cabinet delete shares).
- M2: a review archive's fields are `archive_fields`, the rule `archive_and_add` writes.
- L1: a state `parse_enum` does not read as archived never reaches `history_refusal` (T3c: refused by the parser).
- L3/L4: a history fact is stable and archival (S4-D27, the arbiter's decision).
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ContextPriority, ExpiryKind, FactDomain, FactEntity, FactState, FactType, TemporalClass
from src.domain.exceptions import FactResolutionError
from src.domain.fact_lifecycle import archive_fields, cabinet_invalidation, walked_to_unseen_text
from src.domain.fact_review_answers import history_fact, history_moved_refusal
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_reasons import answer_reason
from src.domain.fact_update import parse_fact_update
from src.domain.fact_update_lifecycle import finish_fact_update
from src.domain.fact_update_plan import plan_fact_update
from tests.support.fact_write import ctx

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
ENDED = answer_reason(AnswerKind.ENDED, "q7", "digit")


def fact(state=FactState.STALE, **over):
    base = dict(
        id="f-a", account_id="acc", created_by_user_id="u1", lineage_id="lin", text="works at Acme",
        type=FactType.STATE, domain=FactDomain.WORK, tags=["job"], temporal_class=TemporalClass.EPHEMERAL,
        context_priority=ContextPriority.CRITICAL, state=state, reported_date=NOW - timedelta(days=200),
        expiry_kind=ExpiryKind.HORIZON, ttl_days=90, expires_at=NOW - timedelta(days=110),
        stale_since=NOW - timedelta(days=9) if state is FactState.STALE else None,
        lifecycle_events=[{"at": NOW - timedelta(days=9), "from": "current", "to": "stale", "actor": "janitor",
                           "reason": "due"}],
    )
    base.update(over)
    return FactEntity(**base)


# -------------------------------------------------------------------------------------------------------- M1

def test_a_history_archive_on_a_rewritten_fact_is_refused():
    rewritten = fact(id="f-b", text="works at Beta")

    refusal = history_moved_refusal("works at Acme", rewritten, "f-a")

    assert refusal is not None and "f-a was rewritten since the owner was asked (now f-b)" in refusal
    assert "nothing was written" in refusal


@pytest.mark.parametrize(("stored_id", "text"), [("f-a", "works at Beta"), ("f-b", "works at Acme")],
                         ids=["the-requested-document", "a-later-version-with-the-same-text"])
def test_a_history_archive_on_the_text_the_owner_saw_passes(stored_id, text):
    assert history_moved_refusal("works at Acme", fact(id=stored_id, text=text), "f-a") is None


def test_the_cabinet_delete_and_the_history_archive_share_one_rule():
    rewritten = fact(id="f-b", text="works at Beta")

    assert walked_to_unseen_text("works at Acme", rewritten, "f-a") is True
    with pytest.raises(FactResolutionError, match="rewritten after it was shown"):
        cabinet_invalidation("works at Acme", rewritten, "f-a", NOW)
    assert walked_to_unseen_text("works at Acme", fact(id="f-b"), "f-a") is False


# -------------------------------------------------------------------------------------------------------- M2

@pytest.mark.parametrize("actor", ["review", "owner"])
@pytest.mark.parametrize("state", [FactState.STALE, FactState.CURRENT])
@pytest.mark.parametrize("updates", [
    {"state": "archived"},
    {"state": "archived", "expiry": {"kind": "none"}},
    {"state": "archived", "reported_date": "2026-10-20T08:00:00+00:00"},
], ids=["state", "with-expiry", "with-reported-date"])
def test_a_review_archive_is_built_by_archive_fields(actor, state, updates):
    current = fact(state)
    update = parse_fact_update(updates)

    finish = finish_fact_update(plan_fact_update(current, update, NOW), current, update, ctx(actor), NOW, reason=ENDED)

    assert finish.plan.kind == "in_place"
    assert finish.plan.fields == archive_fields(current, NOW, actor, ENDED)
    assert "stale_since" not in finish.plan.fields


# -------------------------------------------------------------------------------------------------------- L1

@pytest.mark.parametrize("state", [5, ["archived"], "archive", "superseded"])
def test_a_state_parse_enum_does_not_read_as_archived_is_refused(state):
    # Reviewer edit (facts S4, T3c): `history_refusal` takes a typed update, whose state is a FactState, so the value
    # cannot reach it; what refuses it is the parser that reads the state (parse_enum), before any update exists. A
    # parsed state other than archived is refused by history_refusal (test_history_fact.py).
    with pytest.raises(ValueError):
        parse_fact_update({"state": state})


# ----------------------------------------------------------------------------------------------------- L3/L4

@pytest.mark.parametrize("temporal_class", list(TemporalClass))
@pytest.mark.parametrize("priority", list(ContextPriority))
def test_a_history_fact_is_stable_and_archival_whatever_the_old_fact_was(temporal_class, priority):
    attrs = history_fact(fact(temporal_class=temporal_class, context_priority=priority), "No longer true: x", NOW, "UTC")

    assert attrs.temporal_class is TemporalClass.STABLE
    assert attrs.context_priority is ContextPriority.ARCHIVAL
