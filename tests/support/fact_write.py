"""Fact-write test helpers (Facts S3, plan §3.19/T0): the expiry literals in the shape the model sends, and `ctx`."""
from datetime import date, datetime
from typing import Any, Dict, Final, Optional, Tuple, Union

from src.domain.fact_write_context import FactWriteContext

__all__ = ["EXPIRY_NONE", "ctx", "horizon", "on"]

EXPIRY_NONE: Final[Dict[str, Any]] = {"kind": "none"}


def horizon(days: int) -> Dict[str, Any]:
    return {"kind": "horizon", "days": days}


def on(value: Union[str, date, datetime]) -> Dict[str, Any]:
    """`kind: date` with the last day the fact holds; a date or datetime is cut to its ISO date."""
    day = value.date() if isinstance(value, datetime) else value
    return {"kind": "date", "date": day if isinstance(day, str) else day.isoformat()}


def ctx(
    actor: str = "stage1",
    timezone: str = "Europe/Paris",
    span: Optional[Tuple[datetime, datetime]] = None,
    model_dates: bool = True,
) -> FactWriteContext:
    """A `FactWriteContext`; the defaults are a Stage 1 pass in the owner's Paris calendar with no message span."""
    return FactWriteContext(actor=actor, timezone=timezone, span=span, model_dates=model_dates)
