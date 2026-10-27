"""Named instants for clock tests (P16): DST transitions, month ends, UTC midnights, and a pinned ``datetime``.

Every instant is written once here; a test imports it and never re-types the number. The table is checked
against ``zoneinfo`` by ``tests/unit/support/test_clock_instants.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any

import pytest

UTC = timezone.utc
_REAL_DATETIME = datetime  # captured once at import: a re-pin must not stack on a pinned class
BERLIN = "Europe/Berlin"
HELSINKI = "Europe/Helsinki"
NEW_YORK = "America/New_York"
ZONES = (BERLIN, HELSINKI, NEW_YORK)


@dataclass(frozen=True)
class Instant:
    """One moment: ``utc`` is aware UTC; ``zone`` is the IANA zone the note is about."""

    name: str
    utc: datetime
    zone: str
    note: str


@dataclass(frozen=True)
class WallTime:
    """A local wall time that has no UTC instant (it falls in the skipped spring-forward hour)."""

    name: str
    naive: datetime
    zone: str
    note: str


@dataclass(frozen=True)
class RepeatedHour:
    """A fall-back HH:30 that happens twice: ``fold0`` is the first pass, ``fold1`` the second."""

    zone: str
    wall: datetime  # naive local wall time, the same for both folds
    fold0: Instant
    fold1: Instant


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


_SPRING = {BERLIN: _utc(2026, 3, 29, 1), HELSINKI: _utc(2026, 3, 29, 1), NEW_YORK: _utc(2026, 3, 8, 7)}
_FALL = {BERLIN: _utc(2026, 10, 25, 1), HELSINKI: _utc(2026, 10, 25, 1), NEW_YORK: _utc(2026, 11, 1, 6)}


def _build_transitions() -> dict[tuple[str, str, str], Instant]:
    table: dict[tuple[str, str, str], Instant] = {}
    for kind, source in (("spring", _SPRING), ("fall", _FALL)):
        for zone, t in source.items():
            offsets = [("T-1m", timedelta(minutes=-1)), ("T", timedelta(0)), ("T+1m", timedelta(minutes=1))]
            if kind == "fall":
                offsets.append(("T+30m", timedelta(minutes=30)))
            for label, delta in offsets:
                table[(zone, kind, label)] = Instant(
                    name=f"{zone} {kind} {label}",
                    utc=t + delta,
                    zone=zone,
                    note=f"{label} around the {kind} transition of {zone}",
                )
    return table


TRANSITIONS: dict[tuple[str, str, str], Instant] = _build_transitions()


def transition(zone: str, kind: str, label: str) -> Instant:
    """``kind`` is ``spring`` or ``fall``; ``label`` one of ``T-1m``, ``T``, ``T+1m``, ``T+30m`` (fall only)."""
    return TRANSITIONS[(zone, kind, label)]


SKIPPED_HOUR: dict[str, WallTime] = {
    BERLIN: WallTime("berlin skipped 02:30", datetime(2026, 3, 29, 2, 30), BERLIN, "no such local time"),
    HELSINKI: WallTime("helsinki skipped 03:30", datetime(2026, 3, 29, 3, 30), HELSINKI, "no such local time"),
    NEW_YORK: WallTime("new_york skipped 02:30", datetime(2026, 3, 8, 2, 30), NEW_YORK, "no such local time"),
}


def _repeated(zone: str, wall: datetime, f0: datetime, f1: datetime) -> RepeatedHour:
    label = wall.strftime("%H:%M")
    return RepeatedHour(
        zone=zone,
        wall=wall,
        fold0=Instant(f"{zone} {label} fold0", f0, zone, "first pass of the repeated hour"),
        fold1=Instant(f"{zone} {label} fold1", f1, zone, "second pass of the repeated hour"),
    )


REPEATED_HOUR: dict[str, RepeatedHour] = {
    BERLIN: _repeated(BERLIN, datetime(2026, 10, 25, 2, 30), _utc(2026, 10, 25, 0, 30), _utc(2026, 10, 25, 1, 30)),
    HELSINKI: _repeated(HELSINKI, datetime(2026, 10, 25, 3, 30), _utc(2026, 10, 25, 0, 30), _utc(2026, 10, 25, 1, 30)),
    NEW_YORK: _repeated(NEW_YORK, datetime(2026, 11, 1, 1, 30), _utc(2026, 11, 1, 5, 30), _utc(2026, 11, 1, 6, 30)),
}


def fold1_anchors(zone: str) -> list[Instant]:
    """Fall ``T``, ``T+1m`` and ``T+30m``: local times in the second pass of the repeated hour."""
    return [transition(zone, "fall", label) for label in ("T", "T+1m", "T+30m")]


def fold0_anchors(zone: str) -> list[Instant]:
    """Fall ``T-1m`` and the first-pass HH:30."""
    return [transition(zone, "fall", "T-1m"), REPEATED_HOUR[zone].fold0]


def _month_end(name: str, last: datetime) -> tuple[Instant, Instant]:
    return (
        Instant(f"{name} last second", last, "UTC", "last second of the month"),
        Instant(f"{name} next second", last + timedelta(seconds=1), "UTC", "first second of the next month"),
    )


FEB_END, FEB_NEXT = _month_end("2026-02", _utc(2026, 2, 28, 23, 59, 59))  # 2026 is not a leap year
OCT_END, OCT_NEXT = _month_end("2026-10", _utc(2026, 10, 31, 23, 59, 59))
DEC_END, DEC_NEXT = _month_end("2026-12", _utc(2026, 12, 31, 23, 59, 59))
MONTH_ENDS = (FEB_END, FEB_NEXT, OCT_END, OCT_NEXT, DEC_END, DEC_NEXT)

UTC_MIDNIGHT = Instant("utc midnight 2026-10-27", _utc(2026, 10, 27, 0), "UTC", "Helsinki is already two hours into the day")
UTC_MIDNIGHT_LOCAL: dict[str, datetime] = {
    BERLIN: datetime(2026, 10, 27, 1, 0),  # CET
    HELSINKI: datetime(2026, 10, 27, 2, 0),  # EET
    NEW_YORK: datetime(2026, 10, 26, 20, 0),  # EDT, the previous day
}

SUMMER_MIDNIGHT = Instant("utc midnight 2026-07-15", _utc(2026, 7, 15, 0), "UTC", "summer offsets")
SUMMER_MIDNIGHT_LOCAL: dict[str, datetime] = {
    BERLIN: datetime(2026, 7, 15, 2, 0),
    HELSINKI: datetime(2026, 7, 15, 3, 0),
    NEW_YORK: datetime(2026, 7, 14, 20, 0),
}


class _RealInstanceMeta(type):
    """``isinstance(x, pinned)`` answers as for the real ``datetime``, so a value from the pinned ``now()`` passes."""

    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, _REAL_DATETIME)


def pin_datetime(monkeypatch: pytest.MonkeyPatch, module: ModuleType, instant: Instant | datetime) -> None:
    """Make ``module.datetime.now()`` / ``utcnow()`` answer ``instant``. Call again to move time.

    ``now(tz)`` takes ``tz`` positionally or by keyword; with no ``tz`` it returns naive UTC.
    """
    at = instant.utc if isinstance(instant, Instant) else instant
    if at.tzinfo is None:
        raise ValueError("pin_datetime needs an aware instant")
    at = at.astimezone(UTC)

    class _Pinned(_REAL_DATETIME, metaclass=_RealInstanceMeta):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return at.astimezone(tz) if tz is not None else at.replace(tzinfo=None)

        @classmethod
        def utcnow(cls) -> datetime:
            return at.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", _Pinned)
