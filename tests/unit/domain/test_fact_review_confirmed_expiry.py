"""What a confirmation keeps of a fact's own expiry decision (Facts S4 · T1c review M1): the owner's yes, a silence and
"I don't remember" all follow one rule, `keeps_expiry` / `confirmed_expiry`.

Reviewer edit (facts S4, T3c): `confirmed_expiry` returns the decision as an `Expiry` (the typed write path's input), not
the model's `{kind, ...}` dict; each expected dict is the Expiry it parsed to, and the write path is compute + born-due
with no parse in front."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactState, FactType
from src.domain.fact_expiry import Expiry
from src.domain.exceptions import FactBornDueError
from src.domain.fact_lifecycle import born_due, compute_expiry, resolves_too_soon
from src.domain.pass_actor import PassActor
from src.domain.fact_review_rules import confirmed_expiry, fallback_fields, fallback_window_days, keeps_expiry
from tests.support.review_rule_builders import NOW, fact, question

DAY = timedelta(days=1)
MADRID = "Europe/Madrid"
NONE = dict(expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None)
AHEAD = compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 28)), NOW, MADRID)["expires_at"]


def dated(expires_at):
    return fact(state=FactState.CURRENT, expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=expires_at)


# ------------------------------------------------------------------------------------------------ the rule

@pytest.mark.parametrize("name, stale, keeps", [
    ("permanent", fact(**NONE), True),
    ("permanent, current", fact(state=FactState.CURRENT, stale_since=None, **NONE), True),
    ("directive with a stale horizon", fact(domain=FactDomain.AGENT_DIRECTIVE), True),
    ("principle with a stale horizon", fact(type=FactType.PRINCIPLE), True),
    ("mindset tag, any case", fact(tags=["MindSet"]), True),
    ("date ahead", dated(AHEAD), True),
    ("date due now", dated(NOW), False),
    ("date past", dated(NOW - DAY), False),
    ("date one microsecond ahead", dated(NOW + timedelta(microseconds=1)), False),
    ("date in its last day, 12 hours ahead", dated(NOW + timedelta(hours=12)), False),
    ("date exactly a day ahead", dated(NOW + DAY), False),
    ("date a day and a second ahead", dated(NOW + DAY + timedelta(seconds=1)), True),
    ("date without its moment", dated(None), False),
    ("due horizon", fact(), False),
    ("legacy fact with no kind", fact(expiry_kind=None, ttl_days=None, expires_at=None), False),
])
def test_which_decisions_a_confirmation_keeps(name, stale, keeps):
    assert keeps_expiry(stale, NOW) is keeps


def test_a_naive_moment_is_read_as_utc():
    assert keeps_expiry(dated(AHEAD), NOW.replace(tzinfo=None)) is True


@pytest.mark.parametrize("stale, expected", [
    (fact(**NONE), Expiry(ExpiryKind.NONE)),
    (fact(domain=FactDomain.AGENT_DIRECTIVE), Expiry(ExpiryKind.NONE)),
    (fact(domain=FactDomain.AGENT_DIRECTIVE, expiry_kind=ExpiryKind.DATE, expires_at=AHEAD, ttl_days=None),
     Expiry(ExpiryKind.NONE)),
    (dated(AHEAD), Expiry(ExpiryKind.DATE, on=date(2026, 10, 28))),
    (dated(NOW - DAY), Expiry(ExpiryKind.HORIZON, days=118)),  # 59 days given since it was reported, doubled
    (fact(), Expiry(ExpiryKind.HORIZON, days=28)),
])
def test_the_expiry_a_confirmation_sends(stale, expected):
    assert confirmed_expiry(stale, NOW, MADRID) == expected


def test_the_horizon_is_the_rules_own_window_not_a_copy():
    stale = fact()
    assert confirmed_expiry(stale, NOW, MADRID).days == fallback_window_days(stale)


# ------------------------------------------------------------------------------------------------ the fallback

@pytest.mark.parametrize("actor", ["timeout", "owner"])
@pytest.mark.parametrize("stale", [
    fact(**NONE), dated(AHEAD), fact(state=FactState.CURRENT, stale_since=None, **NONE),
], ids=["permanent", "date ahead", "permanent, current"])
def test_a_revive_fallback_leaves_a_kept_decision_as_it_is(stale, actor):
    fields = fallback_fields(stale, question(), NOW, actor)
    assert not {"expiry_kind", "ttl_days", "expires_at"} & set(fields)
    revived = stale.model_copy(update=fields)
    assert (revived.expiry_kind, revived.ttl_days, revived.expires_at) == (stale.expiry_kind, stale.ttl_days, stale.expires_at)
    assert (revived.state, revived.stale_since) == (FactState.CURRENT, None)
    assert fields["lifecycle_events"][-1]["to"] == "current"
    assert fields["lifecycle_events"][-1]["actor"] == actor


@pytest.mark.parametrize("stale", [fact(), dated(NOW - DAY), fact(expiry_kind=None, ttl_days=None, expires_at=None)],
                         ids=["horizon", "date past", "legacy"])
def test_a_revive_fallback_still_gives_the_window_to_anything_else(stale):
    fields = fallback_fields(stale, question(), NOW, "timeout")
    assert fields["expiry_kind"] is ExpiryKind.HORIZON
    days = fallback_window_days(stale)
    assert days * DAY <= fields["expires_at"] - NOW < (days + 1) * DAY


def test_an_archive_fallback_never_touches_the_expiry():
    fields = fallback_fields(fact(**NONE), question(fallback="archive"), NOW, "timeout")
    assert fields["state"] is FactState.ARCHIVED
    assert not {"expiry_kind", "ttl_days", "expires_at"} & set(fields)


# ------------------------------------------------------------------------------------------------ the write path's own bound (R1)

def test_the_bound_is_the_write_paths_one_day_margin():
    assert resolves_too_soon(NOW + DAY, NOW) is True
    assert resolves_too_soon(NOW + DAY + timedelta(microseconds=1), NOW) is False
    assert resolves_too_soon(NOW, NOW) is True


def _write_accepts(sent, reported, now, tz):
    """What the write path does with an expiry a confirmation sent: compute, refuse when born due (typed: no parse)."""
    born_due(compute_expiry(sent, reported, tz), reported, now, tz, PassActor.OWNER)


@pytest.mark.parametrize("name, now, expires_at, tz", [
    ("12 hours ahead", NOW, NOW + timedelta(hours=12), MADRID),
    ("exactly a day ahead", NOW, NOW + DAY, MADRID),
    ("a day and a second ahead", NOW, NOW + DAY + timedelta(seconds=1), MADRID),
    ("two days ahead", NOW, NOW + 2 * DAY, MADRID),
    ("a plan ending today", NOW, compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 24)), NOW, MADRID)["expires_at"], MADRID),
    ("a plan ending tomorrow", NOW, compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 25)), NOW, MADRID)["expires_at"], MADRID),
    ("a plan ending the day after", NOW, compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 26)), NOW, MADRID)["expires_at"], MADRID),
    # the 23-hour day of the spring change (29 Mar 2026 in Europe/Madrid) and the 25-hour day of the autumn one (25 Oct)
    ("spring: plan ending the 23-hour day, asked at 01:30 before it",
     datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc),
     compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 3, 29)), NOW, MADRID)["expires_at"], MADRID),
    ("spring: plan ending the day after the change",
     datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc),
     compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 3, 30)), NOW, MADRID)["expires_at"], MADRID),
    ("spring: asked in the last hour of the short day",
     datetime(2026, 3, 29, 21, 30, tzinfo=timezone.utc),
     compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 3, 30)), NOW, MADRID)["expires_at"], MADRID),
    ("autumn: plan ending the 25-hour day, asked the evening before",
     datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc),
     compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 25)), NOW, MADRID)["expires_at"], MADRID),
    ("autumn: plan ending the day after the change",
     datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
     compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 26)), NOW, MADRID)["expires_at"], MADRID),
])
def test_what_a_yes_sends_always_passes_the_write_paths_born_due_check(name, now, expires_at, tz):
    plan = dated(expires_at).model_copy(update={"reported_date": now - 30 * DAY})
    sent = confirmed_expiry(plan, now, tz)
    if sent.kind is ExpiryKind.HORIZON:
        assert sent.days >= 14  # counted from now by the review horizon, never born due
    else:
        _write_accepts(sent, plan.reported_date, now, tz)


@pytest.mark.parametrize("expires_at", [NOW + timedelta(hours=12), NOW + DAY, NOW + timedelta(microseconds=1)])
def test_a_date_the_rule_does_not_keep_is_one_the_write_path_would_refuse(expires_at):
    plan = dated(expires_at)
    assert keeps_expiry(plan, NOW) is False
    with pytest.raises(FactBornDueError):
        born_due({"expiry_kind": ExpiryKind.DATE, "expires_at": expires_at}, plan.reported_date, NOW, MADRID, PassActor.OWNER)


def test_a_date_in_its_last_day_gets_the_window_on_both_paths():
    last_day = dated(NOW + timedelta(hours=12))
    assert confirmed_expiry(last_day, NOW, MADRID).kind is ExpiryKind.HORIZON
    fields = fallback_fields(last_day, question(), NOW, "timeout")
    assert fields["expiry_kind"] is ExpiryKind.HORIZON


# ------------------------------------------------------------------------------------------------ a pinned fact is permanent on both paths (R2)

PINNED = [{"domain": FactDomain.AGENT_DIRECTIVE}, {"type": FactType.PRINCIPLE}, {"tags": ["mindset"]}]


@pytest.mark.parametrize("pinned", PINNED)
def test_a_yes_leaves_a_pinned_fact_that_had_a_horizon_permanent(pinned):
    stale = fact(**pinned)  # a horizon with a past due date
    sent = confirmed_expiry(stale, NOW, MADRID)
    stored = compute_expiry(sent, stale.reported_date, MADRID)
    assert stored == {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}


@pytest.mark.parametrize("actor", ["timeout", "owner"])
@pytest.mark.parametrize("pinned", PINNED)
def test_a_silence_leaves_a_pinned_fact_that_had_a_horizon_permanent(pinned, actor):
    stale = fact(**pinned)
    assert stale.expiry_kind is ExpiryKind.HORIZON
    revived = stale.model_copy(update=fallback_fields(stale, question(), NOW, actor))
    assert (revived.expiry_kind, revived.ttl_days, revived.expires_at) == (ExpiryKind.NONE, None, None)
    assert revived.state is FactState.CURRENT


def test_a_pinned_fact_with_a_date_ahead_is_permanent_too():
    plan = dated(AHEAD).model_copy(update={"domain": FactDomain.AGENT_DIRECTIVE})
    revived = plan.model_copy(update=fallback_fields(plan, question(), NOW, "timeout"))
    assert revived.expiry_kind is ExpiryKind.NONE
    assert confirmed_expiry(plan, NOW, MADRID) == Expiry(ExpiryKind.NONE)
