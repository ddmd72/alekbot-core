"""Fact-write test helpers (Facts S3, plan §3.19/T0): the expiry literals in the shape the model sends, `ctx`, and
`rendered` (Facts S4 T8e: the model's dict of a typed update outcome)."""
from datetime import date, datetime
from typing import Any, Dict, Final, Optional, Tuple, Union

from src.domain.fact_tool_result import update_result
from src.domain.fact_update_outcome import FactUpdateOutcome
from src.domain.fact_write_context import FactWriteContext
from src.domain.pass_actor import PassActor

__all__ = ["EXPIRY_NONE", "ctx", "horizon", "on", "rendered"]

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
) -> FactWriteContext:
    """A `FactWriteContext`; the defaults are a Stage 1 pass in the owner's Europe/Paris calendar with no message span. The
    pass's rules (`model_dates` and the rest) come from its `PassActor` row."""
    return FactWriteContext(actor=PassActor(actor), timezone=timezone, span=span)


def rendered(outcome: Any) -> Dict[str, Any]:
    """The model's `update_fact` dict of a typed update outcome, through the one renderer (`update_result`), for a test
    that reads the result in the dict's words. Fails unless the write path returned the typed outcome."""
    assert isinstance(outcome, FactUpdateOutcome), outcome
    return update_result(outcome)
