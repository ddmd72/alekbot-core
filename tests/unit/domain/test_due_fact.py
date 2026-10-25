"""`DueFact`, the janitor's projected record (Facts S3 · T3)."""
import dataclasses
from datetime import datetime, timezone

import pytest

from src.domain.due_fact import DueFact
from src.domain.entities import ExpiryKind

DUE = datetime(2026, 6, 1, tzinfo=timezone.utc)


def due(**over):
    fields = dict(fact_id="f1", account_id="acc", expires_at=DUE, expiry_kind=ExpiryKind.DATE, update_time=object())
    fields.update(over)
    return DueFact(**fields)


def test_a_due_fact_carries_the_projection_and_the_change_token():
    token = object()
    d = due(update_time=token)
    assert (d.fact_id, d.account_id, d.expires_at, d.expiry_kind) == ("f1", "acc", DUE, ExpiryKind.DATE)
    assert d.update_time is token


def test_a_due_fact_holds_no_text_and_no_vectors():
    assert {f.name for f in dataclasses.fields(DueFact)} == {
        "fact_id", "account_id", "expires_at", "expiry_kind", "update_time", "lifecycle_events"}


def test_it_is_frozen():
    d = due()
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.account_id = "other"  # type: ignore[misc]


def test_the_trail_defaults_to_empty_and_is_not_shared_between_instances():
    first, second = due(), due()
    assert first.lifecycle_events == [] and first.lifecycle_events is not second.lifecycle_events


def test_a_document_written_before_the_expiry_model_has_no_kind():
    assert due(expiry_kind=None).expiry_kind is None


def test_equality_compares_every_field_including_the_token_and_the_trail():
    token = object()
    assert due(update_time=token) == due(update_time=token)
    assert due(update_time=token) != due(update_time=object())
    assert due(update_time=token) != due(update_time=token, lifecycle_events=[{"to": "stale"}])
    assert due(update_time=token) != due(update_time=token, account_id="other")
