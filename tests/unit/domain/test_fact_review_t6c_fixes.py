"""T6c fix round, domain side: `born_due` refuses a kind that is no member (S2), the one fact view over the stored fields
(S3), and `check_labels` owns the not-a-mapping refusal (S7)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.domain.entities import ExpiryKind, FactEntity, FactType
from src.domain.fact_lifecycle import born_due, compute_expiry
from src.domain.fact_expiry import Expiry
from src.domain.fact_review_language import CANONICAL_LABELS, check_labels
from src.domain.pass_actor import PassActor
from src.domain.search import EnrichedFact, consolidation_hit, fact_view

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ S2

@pytest.mark.parametrize("kind", ["date", "horizon", 1])
def test_born_due_refuses_a_kind_that_is_not_a_member(kind):
    fields = {"expiry_kind": kind, "ttl_days": None, "expires_at": NOW - timedelta(days=3)}
    with pytest.raises(ValueError, match="ExpiryKind"):
        born_due(fields, NOW - timedelta(days=10), NOW, "UTC", PassActor.STAGE2A)


def test_born_due_still_reads_members_and_none():
    due = compute_expiry(Expiry(ExpiryKind.DATE, on=(NOW - timedelta(days=3)).date()), NOW - timedelta(days=10), "UTC")
    with pytest.raises(ValueError, match="date"):          # the date refusal, not the kind refusal
        born_due(due, NOW - timedelta(days=10), NOW, "UTC", PassActor.STAGE1)
    fields = {"expiry_kind": None, "ttl_days": None, "expires_at": None}
    assert born_due(fields, NOW, NOW, "UTC", PassActor.STAGE1) == (fields, None)


# ------------------------------------------------------------------------------------------------ S3

STORED = FactEntity(
    id="f1", account_id="a", created_by_user_id="u", lineage_id="f1", text="takes the pills daily", type=FactType.EVENT,
    reported_date=datetime(2026, 10, 22, 23, 30, tzinfo=UTC), expiry_kind=ExpiryKind.HORIZON, ttl_days=30,
    expires_at=datetime(2026, 11, 21, 23, 30, tzinfo=UTC),
)


def test_a_search_hit_shows_the_same_view_as_its_stored_fact():
    hit = consolidation_hit(EnrichedFact.from_fact(STORED, "keyword", 0.8), "Europe/Paris")
    view = fact_view(STORED, "Europe/Paris")
    assert {k: hit[k] for k in view} == view


def test_the_view_reads_a_stored_fact_by_its_fields():
    """Anything with the stored fact's fields (a test double for a FactEntity) gives the same view."""
    double = SimpleNamespace(**{name: getattr(STORED, name) for name in
                                ("id", "text", "expiry_kind", "ttl_days", "expires_at", "reported_date")})
    assert fact_view(double, "Europe/Paris") == fact_view(STORED, "Europe/Paris")  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ S7

@pytest.mark.parametrize("labels", [None, "Да / Нет", ["Да"], 3])
def test_labels_that_are_not_a_mapping_are_refused_by_the_domain(labels):
    assert check_labels(labels) == "the labels are not an object"


def test_the_canonical_labels_still_pass():
    assert check_labels(dict(CANONICAL_LABELS)) is None
