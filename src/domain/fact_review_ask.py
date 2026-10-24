"""The review model's `ask_owner` call (plan §3.5): one fact, what silence does to it, and why it may be archived."""
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional

from .fact_review_basis import parse_day

_SHAPE = "ask_owner takes {fact_id, fallback: revive|archive, fallback_date?: YYYY-MM-DD, noise?: true}"


@dataclass(frozen=True)
class AskRequest:
    """The arguments of one `ask_owner` call, as given; `ask_refusal` says whether they may be asked."""

    fact_id: str
    fallback: str
    fallback_date: Optional[date] = None
    noise: bool = False


def parse_ask_owner(raw: object) -> AskRequest:
    """The model's `ask_owner` arguments. Raises ValueError whose text names the argument and its shape. Unknown keys are
    ignored (the model writes no question text); `noise` accepts true/false as text too."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"ask_owner arguments must be an object: {_SHAPE}")
    fact_id, fallback = raw.get("fact_id"), raw.get("fallback")
    if not isinstance(fact_id, str) or not fact_id.strip():
        raise ValueError(f"ask_owner.fact_id must be the id of a fact in the batch: {_SHAPE}")
    if not isinstance(fallback, str):
        raise ValueError(f"ask_owner.fallback must be revive or archive: {_SHAPE}")
    return AskRequest(fact_id.strip(), fallback.strip().lower(), parse_day(raw.get("fallback_date"), "ask_owner.fallback_date"), _flag(raw.get("noise")))


def _flag(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"ask_owner.noise must be true or false, got {value!r}")
