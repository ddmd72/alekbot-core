"""The history fact written beside an archive (facts S4 · T3a, S4-D27): `history_fact`, its trail entry (`history_trail`,
`history_reason`) and when a typed update may carry one (`history_refusal`)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import (
    ContextPriority, ExpiryKind, FactDomain, FactEntity, FactState, FactType, TemporalClass,
)
from src.domain.fact_expiry import EXPIRY_NONE
from src.domain.fact_review_answers import ARCHIVE_ONLY, ended_text, history_fact, history_refusal, history_trail
from src.domain.fact_review_reasons import gives_window_reason, history_reason, is_revive_reason, is_rewrite_reason
from src.domain.fact_update import ParsedFactUpdate, parse_fact_update
from tests.support.fact_write import ctx

UTC = timezone.utc
SAID = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)
NOW = SAID + timedelta(minutes=3)


def old_fact(**over):
    base = dict(
        id="f-old", account_id="acc", created_by_user_id="u1", lineage_id="lin-old", text="works at Acme",
        type=FactType.STATE, domain=FactDomain.WORK,
        tags=["job", "consolidated"], metadata={"employer": "Acme"}, temporal_class=TemporalClass.DYNAMIC,
        context_priority=ContextPriority.HIGH, state=FactState.STALE, reported_date=SAID - timedelta(days=400),
        expiry_kind=ExpiryKind.HORIZON, ttl_days=180, expires_at=SAID - timedelta(days=220),
        lifecycle_events=[{"at": SAID, "from": None, "to": "current", "actor": "stage1", "reason": "x"}],
    )
    base.update(over)
    return FactEntity(**base)


# ------------------------------------------------------------------------------------------------- history_fact

def test_the_history_fact_says_the_text_holds_forever_and_keeps_the_old_facts_taxonomy():
    old = old_fact()
    text = ended_text(old.text, SAID, "Europe/Madrid")

    attrs = history_fact(old, text, SAID, "Europe/Madrid")

    assert attrs.content == text and attrs.state is FactState.CURRENT
    assert attrs.expiry == EXPIRY_NONE
    assert attrs.expiry_fields == {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}
    assert (attrs.domain, attrs.type, attrs.tags) == (old.domain, old.type, old.tags)
    # Reviewer edit (facts S4, T3a re-review, L3/L4 arbiter): a history fact is stable and archival, not copied.
    assert (attrs.temporal_class, attrs.context_priority) == (TemporalClass.STABLE, ContextPriority.ARCHIVAL)
    assert attrs.reported_date == SAID
    assert attrs.metadata == {} and attrs.context is None and attrs.derived_from == ()


def test_the_history_facts_tags_are_a_copy_the_builder_may_extend():
    old = old_fact()

    attrs = history_fact(old, "No longer true: works at Acme", SAID, "UTC")
    attrs.tags.append("anchor")

    assert old.tags == ["job", "consolidated"]


def test_the_reported_date_is_stored_in_utc():
    plus2 = SAID.astimezone(timezone(timedelta(hours=2)))

    attrs = history_fact(old_fact(), "No longer true: works at Acme", plus2, "Europe/Madrid")

    assert attrs.reported_date == SAID and attrs.reported_date.utcoffset() == timedelta(0)


@pytest.mark.parametrize("text", ["", "   "])
def test_an_empty_history_text_is_refused(text):
    with pytest.raises(ValueError, match="needs its text"):
        history_fact(old_fact(), text, SAID, "UTC")


# ------------------------------------------------------------------------------------------------ history_trail

def test_the_history_fact_is_born_current_with_its_own_reason_under_the_actor():
    born = old_fact(id="f-new", lineage_id="lin-new", state=FactState.CURRENT, lifecycle_events=[])

    traced = history_trail(born, "f-old", NOW, "owner")

    assert traced.lifecycle_events == [
        {"at": NOW, "from": None, "to": "current", "actor": "owner", "reason": history_reason("f-old")}
    ]
    assert born.lifecycle_events == []


def test_the_history_reason_names_the_archived_fact_and_is_no_revive_or_rewrite():
    reason = history_reason("f-old")

    assert reason == "history of f-old"
    assert not is_revive_reason(reason) and not is_rewrite_reason(reason) and not gives_window_reason(reason)


# ---------------------------------------------------------------------------------------------- history_refusal
# Reviewer edit (facts S4, T3c): `history_refusal` judges the typed update (`ParsedFactUpdate`), which only code's
# typed write path carries. The model-argument cases stay, each as the update the parser makes of it (so the case of the
# state string is still pinned, now in the parser); the refusal is reworded for code ("archives the fact and changes
# nothing else"), and typed updates the parser could never produce are added.

@pytest.mark.parametrize("actor", ["review", "owner"])
@pytest.mark.parametrize("state", ["archived", "ARCHIVED", " Archived "])
def test_an_exact_archive_of_the_review_or_the_owner_may_carry_history(actor, state):
    update = parse_fact_update({"state": state})

    assert update == ARCHIVE_ONLY
    assert history_refusal("No longer true: x", update, ctx(actor), SAID) is None


@pytest.mark.parametrize("update", [
    parse_fact_update({"state": "invalidated"}),
    parse_fact_update({"state": "current"}),
    parse_fact_update({"state": "archived", "expiry": {"kind": "none"}}),
    parse_fact_update({"state": "archived", "reported_date": "2026-10-01"}),
    parse_fact_update({"content": "x"}),
    parse_fact_update({}),
    parse_fact_update({"state": None}),
    ParsedFactUpdate(state=FactState.ARCHIVED, expiry=EXPIRY_NONE),
    ParsedFactUpdate(state=FactState.ARCHIVED, tags=["x"]),
    ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="f-new"),
], ids=["invalidated", "current", "archived+expiry", "archived+reported_date", "content", "empty", "state None",
        "typed archived+expiry", "typed archived+tags", "typed superseded"])
def test_anything_but_exactly_state_archived_is_refused(update):
    refusal = history_refusal("No longer true: x", update, ctx("owner"), SAID)

    assert refusal is not None and "archives the fact and changes nothing else" in refusal
    assert "nothing was written" in refusal


@pytest.mark.parametrize("actor", ["stage1", "email", "stage2a", "stage2b", "cluster", "backstop"])
def test_a_consolidation_pass_may_not_write_history(actor):
    refusal = history_refusal("No longer true: x", ARCHIVE_ONLY, ctx(actor), SAID)

    assert refusal is not None and refusal.startswith("history: only the review or the owner") and actor in refusal


def test_an_empty_history_text_is_refused_with_its_own_message():
    refusal = history_refusal("  ", ARCHIVE_ONLY, ctx("review"), SAID)

    assert refusal is not None and "is empty" in refusal


def test_history_without_the_moment_it_was_said_is_refused():
    refusal = history_refusal("No longer true: x", ARCHIVE_ONLY, ctx("owner"), None)

    assert refusal is not None and "evidence_at" in refusal
