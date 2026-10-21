"""FactEntity.reported_date: pydantic parses strings to datetime and rejects garbage."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from src.domain.entities import FactEntity, FactType


def _fact(**kw):
    return FactEntity(
        account_id="acc", created_by_user_id="usr", lineage_id="lin",
        text="t", type=FactType.EVENT, **kw,
    )


def test_iso_with_z_is_tz_aware_datetime():
    d = _fact(reported_date="2026-10-21T10:00:00Z").reported_date
    assert isinstance(d, datetime)
    assert d.tzinfo is not None


def test_naive_iso_gives_datetime():
    assert isinstance(_fact(reported_date="2026-02-17T10:15:37").reported_date, datetime)


def test_model_dump_keeps_datetime():
    dumped = _fact(reported_date="2026-10-21T10:00:00Z").model_dump()
    assert isinstance(dumped["reported_date"], datetime)


def test_unparseable_string_raises():
    with pytest.raises(ValidationError):
        _fact(reported_date="Oct 2026")
