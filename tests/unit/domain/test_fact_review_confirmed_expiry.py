"""What a confirmation keeps of a fact's own expiry decision (Facts S4 · T1c review M1): the owner's yes, a silence and
"I don't remember" all follow one rule, `keeps_expiry` / `confirmed_expiry`."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactState, FactType
from src.domain.fact_expiry import Expiry
from src.domain.fact_lifecycle import compute_expiry
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
    ("date one microsecond ahead", dated(NOW + timedelta(microseconds=1)), True),
    ("date without its moment", dated(None), False),
    ("due horizon", fact(), False),
    ("legacy fact with no kind", fact(expiry_kind=None, ttl_days=None, expires_at=None), False),
])
def test_which_decisions_a_confirmation_keeps(name, stale, keeps):
    assert keeps_expiry(stale, NOW) is keeps


def test_a_naive_moment_is_read_as_utc():
    assert keeps_expiry(dated(AHEAD), NOW.replace(tzinfo=None)) is True


@pytest.mark.parametrize("stale, expected", [
    (fact(**NONE), {"kind": "none"}),
    (fact(domain=FactDomain.AGENT_DIRECTIVE), {"kind": "none"}),
    (fact(domain=FactDomain.AGENT_DIRECTIVE, expiry_kind=ExpiryKind.DATE, expires_at=AHEAD, ttl_days=None), {"kind": "none"}),
    (dated(AHEAD), {"kind": "date", "date": "2026-10-28"}),
    (dated(NOW - DAY), {"kind": "horizon", "days": 118}),  # 59 days given since it was reported, doubled
    (fact(), {"kind": "horizon", "days": 28}),
])
def test_the_expiry_a_confirmation_sends(stale, expected):
    assert confirmed_expiry(stale, NOW, MADRID) == expected


def test_the_horizon_is_the_rules_own_window_not_a_copy():
    stale = fact()
    assert confirmed_expiry(stale, NOW, MADRID)["days"] == fallback_window_days(stale)


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
