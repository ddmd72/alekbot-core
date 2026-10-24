"""`plan_date` (Facts S4 · T6a1): the date a `date_passed` basis says has passed, read by the trail reason and by the basis
rule alike."""
from datetime import date, datetime, timezone

from src.domain.entities import ExpiryKind
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_rules import plan_date
from tests.support.review_rule_builders import fact

PASSED = ReviewBasis("date_passed", on=date(2026, 9, 1))


def test_a_date_fact_uses_its_own_last_day_in_the_owners_zone():
    dated = fact("f1", expiry_kind=ExpiryKind.DATE, ttl_days=None,
                 expires_at=datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc))
    assert plan_date(dated, PASSED, "Europe/Paris") == date(2026, 9, 30)


def test_any_other_fact_uses_the_basis_date():
    assert plan_date(fact("f1"), PASSED, "Europe/Paris") == date(2026, 9, 1)
    assert plan_date(fact("f1"), ReviewBasis("date_passed"), "Europe/Paris") is None
