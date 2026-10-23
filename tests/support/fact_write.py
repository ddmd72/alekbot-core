"""Fact-write test helpers (Facts S3, plan §3.19/T0): the expiry literals in the shape the model sends.

`ctx(...)` (a `FactWriteContext` builder) lands with T2, when that class exists; until then this file holds the literals only.
"""
from datetime import date, datetime
from typing import Any, Dict, Final, Union

__all__ = ["EXPIRY_NONE", "horizon", "on"]

EXPIRY_NONE: Final[Dict[str, Any]] = {"kind": "none"}


def horizon(days: int) -> Dict[str, Any]:
    return {"kind": "horizon", "days": days}


def on(value: Union[str, date, datetime]) -> Dict[str, Any]:
    """`kind: date` with the last day the fact holds; a date or datetime is cut to its ISO date."""
    day = value.date() if isinstance(value, datetime) else value
    return {"kind": "date", "date": day if isinstance(day, str) else day.isoformat()}
