"""The weekly digest of the facts the memory review set aside (Facts S4 plan §3.11, S4-D8): which facts it lists, from
when, how many, and the message it is made into. Pure: the service reads the closed facts, claims the week, sends and marks;
whether the digest is due (`digest_due`) and the week's key (`iso_week_of`) are in `fact_review_rules`.

It lists removals: a fact that is `archived` or `invalidated` whose last trail entry is the review's, a timeout's or
the owner's. A fact the owner ended is listed as archived; the history fact written beside it is `current`, so it is
never a removal and is not listed. P2-3 adds two blocks: the facts a timeout wrote as history (its question's `unknown`
text, `digest_histories`), and the routine invoices waiting for a payment answer in the Cabinet (`digest_invoices`). A
text is quoted as the owner is shown it (`shown_quote`): its translation into the message language when the review
stored one that still holds, else as stored; the lines around it are in the message language."""
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Final, List, Mapping, Optional, Sequence, get_args

from .entities import FactEntity, FactState
from .fact_lifecycle_day import CounterField
from .fact_review_answers import written_text
from .fact_review_question import UNKNOWN_TEXT, ClosedBy, FactReviewQuestion
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
_HISTORY_HEADER = UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value
_INVOICES_HEADER = UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value
_INVOICES_CABINET = UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value


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


def digest_histories(questions: Sequence[FactReviewQuestion], since: datetime) -> List[FactReviewQuestion]:
    """The questions whose silence wrote their fact as history since `since` (closed by a timeout with the `history`
    fallback, `closed_at` after `since`), newest first, at most DIGEST_LIMIT. An owner's answer is not listed."""
    listed = [
        q for q in questions
        if q.closed_by is ClosedBy.TIMEOUT and q.fallback == "history" and q.closed_at is not None and q.closed_at > since
    ]
    return sorted(listed, key=lambda q: (q.closed_at, q.question_id), reverse=True)[:DIGEST_LIMIT]


def digest_invoices(questions: Sequence[FactReviewQuestion]) -> List[FactReviewQuestion]:
    """The open digest questions (routine invoices waiting for a payment answer in the Cabinet), oldest first, at most
    DIGEST_LIMIT."""
    listed = [q for q in questions if q.is_open and q.delivery == "digest"]
    return sorted(listed, key=lambda q: (q.queued_at, q.question_id))[:DIGEST_LIMIT]


def history_line(question: FactReviewQuestion, tz: str) -> str:
    """What a timed-out question wrote as history: its `unknown` text (code-built for a kept-twice question, dated at its
    close in the owner's zone `tz`). ValueError for a question that is not closed."""
    if question.closed_at is None:
        raise ValueError(f"question {question.question_id} is not closed")
    return written_text(question, UNKNOWN_TEXT, question.quoted_text, question.closed_at, tz)


def digest_message(
    labels: Mapping[str, str],
    facts: Sequence[FactEntity],
    translations: Mapping[str, QuoteTranslation] = MappingProxyType({}),
    language: Optional[str] = None,
    *,
    histories: Sequence[str] = (),
    invoices: Sequence[str] = (),
    cabinet_url: Optional[str] = None,
) -> str:
    """The digest text, its sections joined by one blank line, each only when it lists something:

    1. the removals: the header, one line per fact (its text as the owner is shown it, with its state's label), a blank
       line and the footer;
    2. the history header and one line per text a timeout wrote as history (`histories`);
    3. the invoices header, one line per invoice text (`invoices`), then, with a `cabinet_url`, the Cabinet line and the
       URL on its own line (code appends it: a label may not hold a URL).

    Every text is shown through `shown_quote` over the translation in `translations` under its `quote_digest` and
    `language`, on one line. `labels` is the message language's label set. With removals only the text is the S4 one.
    Raises ValueError when there is nothing to list (a digest with nothing is not sent) or for a fact that is neither
    archived nor invalidated."""
    if not facts and not histories and not invoices:
        raise ValueError("a digest lists at least one fact, history or invoice")
    sections: List[str] = []
    if facts:
        lines = []
        for fact in facts:
            key = STATE_LABEL_KEYS.get(fact.state)
            if key is None:
                raise ValueError(f"fact {fact.id} is {fact.state.value}: only archived and invalidated facts are listed")
            lines.append(f"• «{_shown(fact.text, translations, language)}» — {labels[key]}")
        sections.append("\n".join([labels[_HEADER], *lines, "", labels[_FOOTER]]))
    if histories:
        sections.append("\n".join([
            labels[_HISTORY_HEADER], *(f"• «{_shown(text, translations, language)}»" for text in histories),
        ]))
    if invoices:
        block = [labels[_INVOICES_HEADER], *(f"• «{_shown(text, translations, language)}»" for text in invoices)]
        if cabinet_url:
            block += [labels[_INVOICES_CABINET], cabinet_url]
        sections.append("\n".join(block))
    return "\n\n".join(sections)


def _shown(text: str, translations: Mapping[str, QuoteTranslation], language: Optional[str]) -> str:
    return " ".join(shown_quote(text, translations.get(quote_digest(text)), language).split())
