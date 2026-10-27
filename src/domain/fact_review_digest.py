"""The weekly digest of the facts the memory review set aside (Facts S4 plan §3.11, S4-D8): which facts it lists, from
when, how many, and the message it is made into. Pure: the service reads the closed facts, claims the week, sends and marks;
whether the digest is due (`digest_due`) and the week's key (`iso_week_of`) are in `fact_review_rules`.

It lists removals only: a fact that is `archived` or `invalidated` whose last trail entry is the review's, a timeout's or
the owner's. A fact the owner ended is listed as archived; the history fact written beside it is `current`, so it is
never a removal and is not listed. A fact is quoted as the owner is shown it (`shown_quote`): its translation into the message language when the review
stored one that still holds, else as stored; the lines around it are in the message language."""
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Final, List, Mapping, Optional, Sequence, get_args

from .entities import FactEntity, FactState
from .fact_lifecycle_day import CounterField
from .fact_review_quote_translation import QuoteTranslation, quote_digest, shown_quote
from .fact_review_reasons import FallbackActor
from .fact_review_state import FactReviewState
from .local_time import local_date
from .pass_actor import PassActor
from .ui_messages import UIMessage

DIGEST_LIMIT: Final[int] = 50  # closed facts one digest reads (and so lists)
FIRST_DIGEST_WINDOW: Final[timedelta] = timedelta(days=30)  # how far back the first digest looks: no digest before it
# The trail actors whose closes the digest tells the owner about; the janitor's and the consolidator's are not the review's.
LISTED_ACTORS: Final[frozenset[str]] = frozenset({PassActor.REVIEW.value, PassActor.OWNER.value, *get_args(FallbackActor)})
# The text that names why a listed fact was set aside, by its state.
STATE_LABEL_KEYS: Final[Mapping[FactState, str]] = {
    FactState.ARCHIVED: UIMessage.MEMORY_DIGEST_ARCHIVED.value,
    FactState.INVALIDATED: UIMessage.MEMORY_DIGEST_INVALIDATED.value,
}
_HEADER = UIMessage.MEMORY_DIGEST_HEADER.value
_FOOTER = UIMessage.MEMORY_DIGEST_FOOTER.value


def pass_set_aside(counts: Mapping[CounterField, int]) -> bool:
    """Whether a review pass's counts show it archived or invalidated a fact: something for the next digest to tell."""
    return counts.get(CounterField.ARCHIVED_BY_REVIEW, 0) > 0 or counts.get(CounterField.INVALIDATED, 0) > 0


def digest_since(state: FactReviewState, now: datetime) -> datetime:
    """Where the digest's read starts: the last digest, or, with none yet, FIRST_DIGEST_WINDOW before `now`."""
    return state.last_digest_at if state.last_digest_at is not None else now - FIRST_DIGEST_WINDOW


def digest_day(now: datetime, zone: str) -> date:
    """The owner's local day of the run: the day whose ISO week the digest is claimed for (the claim is once per such
    week, so a run just after the owner's midnight belongs to the owner's week, not to UTC's)."""
    return local_date(now, zone)


def last_actor(fact: FactEntity) -> Optional[str]:
    """Who made the fact's latest transition (its last trail entry), or None for a fact with no trail."""
    return str(fact.lifecycle_events[-1].get("actor")) if fact.lifecycle_events else None


def digest_facts(closed: Sequence[FactEntity]) -> List[FactEntity]:
    """The facts of `closed` the digest lists, in the order given (newest closure first): archived or invalidated, closed
    by the review, a timeout or the owner. At most DIGEST_LIMIT."""
    listed = [f for f in closed if f.state in STATE_LABEL_KEYS and last_actor(f) in LISTED_ACTORS]
    return listed[:DIGEST_LIMIT]


def digest_message(
    labels: Mapping[str, str],
    facts: Sequence[FactEntity],
    translations: Mapping[str, QuoteTranslation] = MappingProxyType({}),
    language: Optional[str] = None,
) -> str:
    """The digest text: the header, one line per fact (its text as the owner is shown it, `shown_quote` over the
    translation in `translations` under `quote_digest(fact.text)` and `language`, on one line, with its state's label),
    a blank line and the footer. `labels` is the message language's label set. Raises ValueError for no facts (a digest
    with nothing to list is not sent) or for a fact that is neither archived nor invalidated."""
    if not facts:
        raise ValueError("a digest lists at least one fact")
    lines = []
    for fact in facts:
        key = STATE_LABEL_KEYS.get(fact.state)
        if key is None:
            raise ValueError(f"fact {fact.id} is {fact.state.value}: only archived and invalidated facts are listed")
        shown = shown_quote(fact.text, translations.get(quote_digest(fact.text)), language)
        lines.append(f"• «{' '.join(shown.split())}» — {labels[key]}")
    return "\n".join([labels[_HEADER], *lines, "", labels[_FOOTER]])
