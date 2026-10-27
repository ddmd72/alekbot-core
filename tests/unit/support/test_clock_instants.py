"""The instant table agrees with zoneinfo, and ``pin_datetime`` pins one module only."""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.support import clock_instants as ci

UTC = timezone.utc
KINDS = ("spring", "fall")


def _off(zone: str, instant: ci.Instant):
    return instant.utc.astimezone(ZoneInfo(zone)).utcoffset()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("zone", ci.ZONES)
def test_offset_changes_at_T(zone, kind):
    before = ci.transition(zone, kind, "T-1m")
    at = ci.transition(zone, kind, "T")
    after = ci.transition(zone, kind, "T+1m")
    assert _off(zone, before) != _off(zone, after)
    assert _off(zone, at) == _off(zone, after)
    assert at.utc - before.utc == timedelta(minutes=1)
    assert after.utc - at.utc == timedelta(minutes=1)


@pytest.mark.parametrize("zone", ci.ZONES)
def test_spring_has_no_plus_30m_and_fall_has(zone):
    assert (zone, "spring", "T+30m") not in ci.TRANSITIONS
    assert ci.transition(zone, "fall", "T+30m").utc - ci.transition(zone, "fall", "T").utc == timedelta(minutes=30)


def test_transition_utc_values():
    assert ci.transition(ci.BERLIN, "spring", "T").utc == datetime(2026, 3, 29, 1, tzinfo=UTC)
    assert ci.transition(ci.HELSINKI, "fall", "T").utc == datetime(2026, 10, 25, 1, tzinfo=UTC)
    assert ci.transition(ci.NEW_YORK, "spring", "T").utc == datetime(2026, 3, 8, 7, tzinfo=UTC)
    assert ci.transition(ci.NEW_YORK, "fall", "T").utc == datetime(2026, 11, 1, 6, tzinfo=UTC)


@pytest.mark.parametrize("zone", ci.ZONES)
def test_skipped_wall_time_does_not_round_trip(zone):
    w = ci.SKIPPED_HOUR[zone]
    z = ZoneInfo(zone)
    back = w.naive.replace(tzinfo=z).astimezone(UTC).astimezone(z).replace(tzinfo=None)
    assert back != w.naive


@pytest.mark.parametrize("zone", ci.ZONES)
def test_repeated_hour_reads_same_wall_time_in_both_folds(zone):
    r = ci.REPEATED_HOUR[zone]
    z = ZoneInfo(zone)
    for fold, inst in ((0, r.fold0), (1, r.fold1)):
        assert r.wall.replace(tzinfo=z, fold=fold).astimezone(UTC) == inst.utc
        assert inst.utc.astimezone(z).replace(tzinfo=None) == r.wall
        assert inst.utc.astimezone(z).fold == fold
    assert r.fold1.utc - r.fold0.utc == timedelta(hours=1)


def test_repeated_hour_values():
    assert ci.REPEATED_HOUR[ci.BERLIN].wall == datetime(2026, 10, 25, 2, 30)
    assert ci.REPEATED_HOUR[ci.HELSINKI].wall == datetime(2026, 10, 25, 3, 30)
    assert ci.REPEATED_HOUR[ci.NEW_YORK].wall == datetime(2026, 11, 1, 1, 30)
    assert ci.REPEATED_HOUR[ci.NEW_YORK].fold0.utc == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


@pytest.mark.parametrize("zone", ci.ZONES)
def test_anchors_are_in_the_stated_fold(zone):
    z = ZoneInfo(zone)
    f1 = ci.fold1_anchors(zone)
    assert [a.name.split()[-1] for a in f1] == ["T", "T+1m", "T+30m"]
    assert all(a.utc.astimezone(z).fold == 1 for a in f1)
    f0 = ci.fold0_anchors(zone)
    assert f0[0] is ci.transition(zone, "fall", "T-1m")
    assert f0[1] is ci.REPEATED_HOUR[zone].fold0
    assert all(a.utc.astimezone(z).fold == 0 for a in f0)


def test_month_ends():
    ends = {m.name: m.utc for m in ci.MONTH_ENDS}
    assert ci.FEB_NEXT.utc == datetime(2026, 3, 1, tzinfo=UTC)
    assert ci.OCT_NEXT.utc == datetime(2026, 11, 1, tzinfo=UTC)
    assert ci.DEC_NEXT.utc == datetime(2027, 1, 1, tzinfo=UTC)
    assert ends["2026-02 last second"] == datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC)
    assert len(ci.MONTH_ENDS) == 6


@pytest.mark.parametrize("zone", ci.ZONES)
def test_utc_midnight_local_readings(zone):
    z = ZoneInfo(zone)
    assert ci.UTC_MIDNIGHT.utc.astimezone(z).replace(tzinfo=None) == ci.UTC_MIDNIGHT_LOCAL[zone]
    assert ci.SUMMER_MIDNIGHT.utc.astimezone(z).replace(tzinfo=None) == ci.SUMMER_MIDNIGHT_LOCAL[zone]


def _module() -> types.ModuleType:
    m = types.ModuleType("fake_target")
    m.datetime = datetime  # type: ignore[attr-defined]
    return m


def test_pinned_module_answers_every_now_spelling(monkeypatch):
    mod = _module()
    inst = ci.transition(ci.BERLIN, "fall", "T+1m")
    ci.pin_datetime(monkeypatch, mod, inst)
    assert mod.datetime.now(UTC) == inst.utc
    assert mod.datetime.now(tz=UTC) == inst.utc
    assert mod.datetime.now() == inst.utc.replace(tzinfo=None)
    assert mod.datetime.now().tzinfo is None
    assert mod.datetime.utcnow() == inst.utc.replace(tzinfo=None)
    local = mod.datetime.now(ZoneInfo(ci.BERLIN))
    # a fold=1 reading never compares equal across tzinfos, so compare in UTC
    assert local.astimezone(UTC) == inst.utc and local.utcoffset() == timedelta(hours=1)
    assert isinstance(mod.datetime.now(UTC), datetime)
    assert mod.datetime.fromisoformat("2026-01-02T03:04:05+00:00").year == 2026
    assert mod.datetime.now(UTC) + timedelta(days=1) == inst.utc + timedelta(days=1)


def test_pin_accepts_a_plain_aware_datetime_and_can_be_repinned(monkeypatch):
    mod = _module()
    ci.pin_datetime(monkeypatch, mod, datetime(2026, 1, 1, tzinfo=UTC))
    assert mod.datetime.now(UTC).year == 2026
    ci.pin_datetime(monkeypatch, mod, ci.FEB_NEXT)
    assert mod.datetime.now(UTC) == ci.FEB_NEXT.utc


def test_pin_leaves_other_modules_untouched(monkeypatch):
    mod, other = _module(), _module()
    ci.pin_datetime(monkeypatch, mod, ci.UTC_MIDNIGHT)
    assert other.datetime is datetime
    assert abs(other.datetime.now(UTC) - datetime.now(UTC)) < timedelta(minutes=5)


def test_pin_refuses_a_naive_instant(monkeypatch):
    refused = False
    try:
        ci.pin_datetime(monkeypatch, _module(), datetime(2026, 1, 1))
    except ValueError:
        refused = True
    assert refused


def test_isinstance_against_the_pinned_class_accepts_real_datetimes(monkeypatch):
    mod = _module()
    for instant in (ci.UTC_MIDNIGHT, ci.FEB_NEXT):  # first pin, then a re-pin
        ci.pin_datetime(monkeypatch, mod, instant)
        assert mod.datetime is not datetime
        assert isinstance(datetime(2026, 1, 1, tzinfo=UTC), mod.datetime)
        assert isinstance(mod.datetime.now(UTC), mod.datetime)
        assert isinstance(mod.datetime.now(), mod.datetime)
        assert not isinstance("2026-01-01", mod.datetime)


def test_undo_restores_the_original_class(monkeypatch):
    mod = _module()
    with pytest.MonkeyPatch.context() as mp:
        ci.pin_datetime(mp, mod, ci.UTC_MIDNIGHT)
        ci.pin_datetime(mp, mod, ci.FEB_NEXT)
        assert mod.datetime is not datetime
    assert mod.datetime is datetime
