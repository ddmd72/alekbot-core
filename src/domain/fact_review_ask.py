"""The review model's `ask_owner` call (plan §3.5, P2-3): one fact, the kind of question, the texts each outcome writes,
the other batch facts that are parts of the same plan (`folds`), and what silence does to it."""
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Mapping, Optional, Tuple

from .fact_review_basis import parse_day

_SHAPE = (
    "ask_owner takes {fact_id, question?: still_true|happened|renewed|paid, texts?: {<key>: <text>}, "
    "folds?: [fact_id], fallback: revive|archive|history, noise?: true}"
)


@dataclass(frozen=True)
class AskRequest:
    """The arguments of one `ask_owner` call, as given; `ask_refusal` says whether they may be asked. `fallback_date` is
    still read, so that its refusal can name it."""

    fact_id: str
    fallback: str
    fallback_date: Optional[date] = None
    noise: bool = False
    question: str = "still_true"
    texts: Dict[str, str] = field(default_factory=dict)
    folds: Tuple[str, ...] = ()


def parse_ask_owner(raw: object) -> AskRequest:
    """The model's `ask_owner` arguments. Raises ValueError whose text names the argument and its shape. Unknown keys are
    ignored; `noise` accepts true/false as text too. `question` is lowercased (default `still_true`), `texts` is an
    object of texts (default none), `folds` a list of fact ids, stripped and without repeats."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"ask_owner arguments must be an object: {_SHAPE}")
    fact_id, fallback = raw.get("fact_id"), raw.get("fallback")
    if not isinstance(fact_id, str) or not fact_id.strip():
        raise ValueError(f"ask_owner.fact_id must be the id of a fact in the batch: {_SHAPE}")
    if not isinstance(fallback, str):
        raise ValueError(f"ask_owner.fallback must be revive, archive or history: {_SHAPE}")
    return AskRequest(
        fact_id.strip(),
        fallback.strip().lower(),
        parse_day(raw.get("fallback_date"), "ask_owner.fallback_date"),
        _flag(raw.get("noise")),
        question=_question(raw.get("question")),
        texts=_texts(raw.get("texts")),
        folds=_folds(raw.get("folds")),
    )


def _flag(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"ask_owner.noise must be true or false, got {value!r}")


def _question(value: Any) -> str:
    if value is None:
        return "still_true"
    if not isinstance(value, str):
        raise ValueError(f"ask_owner.question must be still_true, happened, renewed or paid: {_SHAPE}")
    return value.strip().lower()


def _texts(value: Any) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(text, str) for key, text in value.items()
    ):
        raise ValueError(f"ask_owner.texts must be an object of texts: {_SHAPE}")
    return dict(value)


def _folds(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"ask_owner.folds must be a list of fact ids of this batch: {_SHAPE}")
    return tuple(dict.fromkeys(item.strip() for item in value))
