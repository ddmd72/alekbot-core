"""What the review model names as the reason for a change (plan §3.5): another fact, the owner's reply, or a passed date."""
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal, Mapping, Optional

BasisKind = Literal["evidence", "reply", "date_passed"]
# What a review write does to a fact: `review_action` decides it, the basis rules and the trail reasons read it.
ReviewAction = Literal["revive", "rewrite", "archive", "invalidate", "supersede"]

_OWN_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "evidence": ("evidence_fact_id",),
    "reply": ("question_id", "quote"),
    "date_passed": ("date",),
}
_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_SHAPES: Final[str] = "{kind: evidence, evidence_fact_id}, {kind: reply, question_id, quote} or {kind: date_passed, date?}"


@dataclass(frozen=True)
class ReviewBasis:
    """`evidence`: `evidence_fact_id`. `reply`: `question_id` and a `quote` of the owner's words. `date_passed`: `on`, the
    date of the plan (a `date` fact uses its own). The fields of the other kinds stay unset."""

    kind: BasisKind
    evidence_fact_id: Optional[str] = None
    question_id: Optional[str] = None
    quote: Optional[str] = None
    on: Optional[date] = None

    def __post_init__(self) -> None:
        own = {"evidence": {"evidence_fact_id"}, "reply": {"question_id", "quote"}, "date_passed": {"on"}}[self.kind]
        foreign = [
            name for name in ("evidence_fact_id", "question_id", "quote", "on")
            if name not in own and getattr(self, name) is not None
        ]
        if foreign:
            raise ValueError(f"a {self.kind} basis does not carry {', '.join(foreign)}")


def parse_review_basis(raw: object) -> ReviewBasis:
    """The model's `basis` argument as a ReviewBasis. Raises ValueError whose text names the place and the shape.

    Unknown keys are ignored; a key that belongs to another kind is refused. A missing field is not refused here:
    `basis_refusal` says what the kind needs."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"basis must be an object: {_SHAPES}")
    kind = raw.get("kind")
    if kind not in _OWN_FIELDS:
        raise ValueError(f"basis.kind must be evidence, reply or date_passed, got {kind!r}: {_SHAPES}")
    foreign = [
        name for other, names in _OWN_FIELDS.items() if other != kind for name in names if raw.get(name) is not None
    ]
    if foreign:
        raise ValueError(f"basis of kind {kind} does not take {', '.join(foreign)}: {_SHAPES}")
    if kind == "evidence":
        return ReviewBasis("evidence", evidence_fact_id=_text(raw, "evidence_fact_id"))
    if kind == "reply":
        return ReviewBasis("reply", question_id=_text(raw, "question_id"), quote=_text(raw, "quote"))
    return ReviewBasis("date_passed", on=parse_day(raw.get("date"), "basis.date"))


def parse_day(value: Any, place: str) -> Optional[date]:
    """`YYYY-MM-DD` as a date; None when absent. Raises ValueError naming `place` for anything else."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and _DAY.fullmatch(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{place} must be a date as YYYY-MM-DD, got {value!r}")


def _text(raw: Mapping[str, Any], name: str) -> Optional[str]:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"basis.{name} must be text, got {type(value).__name__}")
    return value.strip() or None
