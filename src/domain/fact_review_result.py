"""What one review pass returns (Facts S4 plan §3.3): the outcome of each fact it wrote, the asks it held, the facts it
kept, the routine invoices it rewrote (P2-2), and, in mirror mode, the language of the owner's messages and the labels it
wrote for it.

`FactReviewResult` is the domain shape; `encode_review_result` and `decode_review_result` are its one form as plain data,
the form the agent task's result carries (in process, as the task itself)."""
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional, Tuple, get_args

from .fact_review_ask import AskRequest
from .fact_review_invoice import InvoiceRequest
from .fact_review_outcome import ReviewDecision, ReviewOutcome
from .fact_review_quote_translation import QuoteTranslation

_OUTCOMES = frozenset(get_args(ReviewOutcome))


@dataclass(frozen=True)
class FactReviewResult:
    """`outcomes`: one per batch fact the pass wrote, in batch order. `asks`: the held asks, each naming its batch fact.
    `kept`: the ids of the batch facts with neither. `message_language` and `labels`: as the pass's final report gave
    them (mirror mode only), None when absent. `translations`: the validated translations of the batch facts' texts into
    the message language (empty when none was wanted or given). `invoices`: the routine invoices the pass rewrote as
    history with `payment` texts, in batch order."""

    outcomes: Tuple[ReviewDecision, ...] = ()
    asks: Tuple[AskRequest, ...] = ()
    kept: Tuple[str, ...] = ()
    message_language: Optional[str] = None
    labels: Optional[Mapping[str, str]] = None
    translations: Tuple[QuoteTranslation, ...] = ()
    invoices: Tuple[InvoiceRequest, ...] = ()


def encode_review_result(result: FactReviewResult) -> Dict[str, Any]:
    """The result as plain data (dicts and lists; an ask's `fallback_date` stays a date, its `folds` a list), read back
    to an equal result by `decode_review_result`."""
    return {
        "outcomes": [asdict(d) for d in result.outcomes],
        "asks": [{**asdict(a), "folds": list(a.folds)} for a in result.asks],
        "kept": list(result.kept),
        "message_language": result.message_language,
        "labels": dict(result.labels) if result.labels is not None else None,
        "translations": [asdict(t) for t in result.translations],
        "invoices": [asdict(i) for i in result.invoices],
    }


def decode_review_result(raw: Optional[Mapping[str, Any]]) -> FactReviewResult:
    """The result an agent task returned; a missing part is empty (None: nothing decided). Raises ValueError for a part
    that is not in `encode_review_result`'s form."""
    if raw is None:
        return FactReviewResult()
    if not isinstance(raw, Mapping):
        raise ValueError("a review result is an object")
    try:
        outcomes = tuple(ReviewDecision(**entry) for entry in raw.get("outcomes") or ())
        asks = tuple(_ask(entry) for entry in raw.get("asks") or ())
        translations = tuple(QuoteTranslation(**entry) for entry in raw.get("translations") or ())
        invoices = tuple(InvoiceRequest(**entry) for entry in raw.get("invoices") or ())
    except (TypeError, ValueError) as error:
        raise ValueError(f"the review result is not valid: {error}") from error
    unknown = sorted({d.outcome for d in outcomes} - _OUTCOMES)
    if unknown:
        raise ValueError(f"the review result names unknown outcomes: {', '.join(unknown)}")
    kept = tuple(raw.get("kept") or ())
    if not all(isinstance(fact_id, str) for fact_id in kept):
        raise ValueError("the review result's kept facts are ids")
    labels = raw.get("labels")
    if labels is not None and not isinstance(labels, Mapping):
        raise ValueError("the review result's labels are an object")
    language = raw.get("message_language")
    if language is not None and not isinstance(language, str):
        raise ValueError("the review result's message_language is a text")
    return FactReviewResult(
        outcomes, asks, kept, language, dict(labels) if labels is not None else None, translations, invoices
    )


def _ask(entry: Mapping[str, Any]) -> AskRequest:
    """An encoded ask: its `folds` back to a tuple and its `texts` to a dict (an entry from before P2 has neither)."""
    fields = dict(entry)
    if "folds" in fields:
        fields["folds"] = tuple(fields["folds"])
    if "texts" in fields:
        fields["texts"] = dict(fields["texts"])
    return AskRequest(**fields)
