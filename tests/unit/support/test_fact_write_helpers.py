from datetime import date, datetime, timezone

from tests.support.fact_write import EXPIRY_NONE, horizon, on


def test_literals_have_the_shape_the_model_sends():
    assert EXPIRY_NONE == {"kind": "none"}
    assert horizon(30) == {"kind": "horizon", "days": 30}
    assert on("2026-11-01") == {"kind": "date", "date": "2026-11-01"}


def test_on_cuts_a_date_or_datetime_to_its_iso_date():
    assert on(date(2026, 11, 1)) == {"kind": "date", "date": "2026-11-01"}
    assert on(datetime(2026, 11, 1, 23, 30, tzinfo=timezone.utc)) == {"kind": "date", "date": "2026-11-01"}


def test_each_call_returns_a_fresh_dict():
    first = horizon(3)
    first["days"] = 99
    assert horizon(3) == {"kind": "horizon", "days": 3}
