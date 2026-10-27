"""What the review model names as the reason for a change (plan §3.5): another fact, the owner's reply, or a passed date.

Since P2 a passed date backs only a history rewrite, and only the owner's reply backs an archive (`basis_refusal`)."""
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Final, Literal, Mapping, Optional, Tuple

from .fact_review_question import TEXT_KEYS

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
    if not isinstance(kind, str) or kind not in _OWN_FIELDS:  # a list or an object is no kind, not a TypeError
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


def parse_history(raw: object) -> Optional[str]:
    """The `history` argument of a review archive: the text of the fact to create beside it, or None when absent. Raises
    ValueError naming the place and the shape for anything that is not text; `basis_refusal` says when it may be sent."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"history must be text: one sentence in the past tense saying it ended, got {type(raw).__name__}")
    return raw


# The two texts of a routine invoice's payment: the keys of a digest `paid` question (`TEXT_KEYS`), the one list.
PAYMENT_KEYS: Final[Tuple[str, ...]] = TEXT_KEYS[("paid", "digest")]
_PAYMENT_SHAPE: Final[str] = "payment must be {" + ", ".join(f"{key}: <text>" for key in PAYMENT_KEYS) + "}"


def parse_payment(raw: object) -> Optional[Dict[str, str]]:
    """The `payment` argument of a routine invoice's history rewrite (P2-2): its paid and not-paid texts, or None when
    absent. Raises ValueError with the shape for anything but an object of exactly those two texts; `payment_refusal`
    checks the texts and says when it may be sent."""
    if raw is None:
        return None
    if (
        not isinstance(raw, Mapping)
        or set(raw) != set(PAYMENT_KEYS)
        or not all(isinstance(value, str) for value in raw.values())
    ):
        raise ValueError(_PAYMENT_SHAPE)
    return {key: raw[key] for key in PAYMENT_KEYS}


def parse_day(value: Any, place: str) -> Optional[date]:
    """`YYYY-MM-DD` as a date; None when absent. Raises ValueError naming `place` for anything else."""
    if value is None or value == "":
        return None
    message = f"{place} must be a date as YYYY-MM-DD, got {value!r}"
    if isinstance(value, str) and _DAY.fullmatch(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as error:  # e.g. 2026-02-30: the right shape, not a real day
            raise ValueError(message) from error
    raise ValueError(message)


def _text(raw: Mapping[str, Any], name: str) -> Optional[str]:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"basis.{name} must be text, got {type(value).__name__}")
    return value.strip() or None
