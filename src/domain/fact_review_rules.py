"""The memory review's rules (FACT_LIFECYCLE_RFC D6/D7, Facts S4 plan §3.2, §3.7, §3.8, §3.11, §3.12, §3.14): which stale facts
go to the model, how the question queue is ordered and when a question may go out, when a question times out and what its
fact becomes then, and when the janitor must start a review. Pure: the services read and write, this decides.

Statuses of a question move only through the methods of `FactReviewQuestion`; nothing here builds a question by hand."""
from datetime import date, datetime, timedelta, timezone
from typing import AbstractSet, Any, Dict, Final, Iterable, List, Literal, Mapping, Optional, Sequence

from .entities import ExpiryKind, FactEntity, FactState
from .fact_expiry import as_expiry_kind, expiry_view
from .fact_lifecycle import horizon_from_now, lifecycle_event, lifecycle_fields, with_event
from .fact_review_basis import ReviewBasis
from .fact_review_question import FactReviewQuestion, QuestionFallback, QuestionStatus
from .fact_review_state import FactReviewState
from .fact_write_context import as_utc
from .local_time import local_date
from .settings import DEFAULT_FACT_REVIEW_INTERVAL_DAYS
from .user import UserBotConfig

REVIEW_BATCH: Final[int] = 20  # stale facts one run puts before the model
MAX_QUESTIONS_PER_DAY: Final[int] = 5  # sent per owner local day
MAX_OFFERS: Final[int] = 2  # a stale fact the model left undecided this often is asked instead of offered again
QUEUE_ALERT: Final[int] = 35  # more queued questions than this raise one alert
POOL_CAP: Final[int] = 50  # stale facts plus facts of open questions: the janitor stales nothing beyond it
MIN_FALLBACK_DAYS: Final[int] = 14
MAX_FALLBACK_DAYS: Final[int] = 365
REVIVE_ALERT: Final[int] = 3  # revives of one fact within REVIVE_WINDOW that raise an alert
REVIVE_WINDOW: Final[timedelta] = timedelta(days=30)
DIGEST_INTERVAL: Final[timedelta] = timedelta(days=7)
# `queue_key` of a fact with no `stale_since`: it queues before every dated one.
EARLIEST_QUEUE_KEY: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)
# What the owner's silence does to a fact the review asked about by itself (no model reason to archive it).
KEPT_TWICE_FALLBACK: Final[QuestionFallback] = "revive"

# Trail reasons. The fallback reasons are built here; the first and second belong to the review's and the owner's writes,
# and every one of the four marks a revive for `revives_in_30_days`.
REASON_REVIVED_ON: Final[str] = "revived on "
REASON_CONFIRMED: Final[str] = "confirmed q="
REASON_NO_ANSWER: Final[str] = "no answer to q="
REASON_DID_NOT_REMEMBER: Final[str] = "did not remember q="
_REVIVE_REASONS: Final[tuple[str, ...]] = (REASON_REVIVED_ON, REASON_CONFIRMED, REASON_NO_ANSWER, REASON_DID_NOT_REMEMBER)

_DAY = timedelta(days=1)

# Why the janitor starts a review, in the order they are tried.
ReviewReason = Literal["stale", "reply", "timeout", "digest", "queue"]
# Who applies a fallback: nobody answered (timeout), or the owner said he does not remember.
FallbackActor = Literal["timeout", "owner"]
QueueAlertAction = Literal["raise", "rearm"]
# What a review write does to a fact, as the basis rules see it.
ReviewAction = Literal["revive", "rewrite", "archive", "invalidate", "supersede"]


# ------------------------------------------------------------------------------------------------ selection

def queue_key(fact: FactEntity) -> datetime:
    """Where a fact stands in line: how long it has been stale (oldest first); no date means the front."""
    return as_utc(fact.stale_since) if fact.stale_since is not None else EARLIEST_QUEUE_KEY


def select_batch(
    stale: Sequence[FactEntity],
    questions: Iterable[FactReviewQuestion],
    reply_facts: Sequence[FactEntity],
    candidates: Sequence[FactEntity],
    offers: Mapping[str, int],
) -> List[FactEntity]:
    """The facts one run puts before the model, at most REVIEW_BATCH.

    First the facts whose question has a recorded reply the review has not read (`reply_facts`) and the timed-out
    candidates; they are current, are not filtered, and keep the order given. Then the stale facts by (offers, longest
    stale first, id): a fact with an open question is the question's business, and one offered MAX_OFFERS times is asked
    instead (`kept_twice`)."""
    held = _open_fact_ids(questions)
    waiting = sorted(
        (f for f in stale if f.id not in held and offers.get(f.id, 0) < MAX_OFFERS),
        key=lambda f: (offers.get(f.id, 0), queue_key(f), f.id),
    )
    chosen: Dict[str, FactEntity] = {}
    for fact in (*reply_facts, *candidates, *waiting):
        chosen.setdefault(fact.id, fact)
    return list(chosen.values())[:REVIEW_BATCH]


def kept_twice(stale: Sequence[FactEntity], offers: Mapping[str, int], questions: Iterable[FactReviewQuestion]) -> List[FactEntity]:
    """The stale facts the model has left undecided MAX_OFFERS times and that have no open question: they are asked
    (never revived by code), the longest stale first."""
    held = _open_fact_ids(questions)
    return sorted(
        (f for f in stale if offers.get(f.id, 0) >= MAX_OFFERS and f.id not in held), key=lambda f: (queue_key(f), f.id)
    )


def _open_fact_ids(questions: Iterable[FactReviewQuestion]) -> frozenset[str]:
    return frozenset(q.fact_id for q in questions if q.is_open)


# ------------------------------------------------------------------------------------------------ the queue

def queue_order(questions: Iterable[FactReviewQuestion]) -> List[FactReviewQuestion]:
    """The order questions go out: the longest-stale fact first (`queue_key`), then the earliest queued, then the id."""
    return sorted(questions, key=lambda q: (q.queue_key, q.queued_at, q.question_id))


def next_in_queue(questions: Iterable[FactReviewQuestion]) -> Optional[FactReviewQuestion]:
    """The first queued question in queue order, or None."""
    return next(iter(queue_order(q for q in questions if q.status is QuestionStatus.QUEUED)), None)


def sent_today(state: FactReviewState, today: date) -> int:
    """Questions sent on the owner's local day `today`; the count of an earlier day is not today's."""
    return state.sent if state.sent_on == today else 0


def may_send(state: FactReviewState, today: date, open_current: bool) -> bool:
    """Whether the next queued question may go out on the local day `today`.

    `open_current`: the question `state.current_question_id` names is still open (a pointer to a closed or missing one
    holds nothing). Not while one is out, not after MAX_QUESTIONS_PER_DAY today, not on the day a delivery failed."""
    if state.current_question_id is not None and open_current:
        return False
    if sent_today(state, today) >= MAX_QUESTIONS_PER_DAY:
        return False
    return state.delivery_failed_on != today


def becomes_unanswered(question: FactReviewQuestion, today: date, tz: str) -> bool:
    """An `asked` question sent on an earlier local day is `unanswered`: still open and timing out, no longer current.
    A recorded reply in words does not keep it current (the review reads it once; undecided, it waits for the timeout)."""
    return (
        question.status is QuestionStatus.ASKED
        and question.asked_at is not None
        and local_date(question.asked_at, tz) < today
    )


# ------------------------------------------------------------------------------------------------ timeouts

def due_timeouts(questions: Iterable[FactReviewQuestion], now: datetime) -> List[FactReviewQuestion]:
    """The questions whose QUESTION_TIMEOUT from `asked_at` has run out (asked and unanswered only), in queue order."""
    return queue_order(q for q in questions if q.is_timed_out(now))


def fallback_window_days(fact: FactEntity) -> int:
    """How long a silence brings a fact back for: twice the window it was last given, between MIN_FALLBACK_DAYS and
    MAX_FALLBACK_DAYS; MIN_FALLBACK_DAYS when it has none. The window runs from the later of `reported_date` and the
    fact's last revive to `expires_at`, so every silence doubles what the last one gave (28, 56, ... days), though a
    horizon is stored from `reported_date`."""
    if fact.expires_at is None:
        return MIN_FALLBACK_DAYS
    window = (as_utc(fact.expires_at) - _last_given(fact)).days
    return min(MAX_FALLBACK_DAYS, max(MIN_FALLBACK_DAYS, 2 * window))


def _last_given(fact: FactEntity) -> datetime:
    revived = (t for e in fact.lifecycle_events if _is_revive(e) and (t := _event_time(e)) is not None)
    return max([as_utc(fact.reported_date), *revived])


def fallback_fields(fact: FactEntity, question: FactReviewQuestion, now: datetime, actor: FallbackActor) -> Dict[str, Any]:
    """The in-place write of a question's fallback, applied when nobody answered or the owner does not remember.

    `archive`: the fact is archived, its `stale_since` kept. `revive`: current again, `stale_since` cleared, a horizon of
    `fallback_window_days` counted from now and stored from `reported_date` (which stays). Either way the trail
    gets one entry under `actor`, with a code-built reason."""
    if fact.state not in (FactState.STALE, FactState.CURRENT):
        raise ValueError(f"a fallback applies to a stale or current fact, not a {fact.state.value} one")
    target = FactState.ARCHIVED if question.fallback == "archive" else FactState.CURRENT
    fields: Dict[str, Any] = lifecycle_fields(target, now)
    if target is FactState.CURRENT:
        fields.update(horizon_from_now(fallback_window_days(fact), fact.reported_date, now))
        fields["stale_since"] = None
    fields["last_updated"] = now
    event = lifecycle_event(now, fact.state, target, actor, _fallback_reason(question, actor))
    fields["lifecycle_events"] = with_event(fact.lifecycle_events, event)
    return fields


def _fallback_reason(question: FactReviewQuestion, actor: FallbackActor) -> str:
    if actor == "owner":
        return f"{REASON_DID_NOT_REMEMBER}{question.question_id}"
    return f"{REASON_NO_ANSWER}{question.question_id}: {question.fallback}"


# ------------------------------------------------------------------------------------------------ revive alert

def revives_in_30_days(events: Sequence[Mapping[str, Any]], now: datetime) -> int:
    """How many times a fact came back to current within REVIVE_WINDOW of `now`, read from its `lifecycle_events`.

    A revive is an entry into `current` whose code-built reason names one (a review revive, an owner's yes, a
    fallback); queueing a fact for the owner is not one. An entry that cannot be read counts for nothing."""
    return sum(1 for e in events if _is_revive(e) and _within(e, now))


def revive_alert_due(events: Sequence[Mapping[str, Any]], now: datetime) -> bool:
    """A fact revived REVIVE_ALERT times or more in the window: its horizon is probably wrong."""
    return revives_in_30_days(events, now) >= REVIVE_ALERT


def _is_revive(event: Mapping[str, Any]) -> bool:
    reason = event.get("reason")
    return event.get("to") == FactState.CURRENT.value and isinstance(reason, str) and reason.startswith(_REVIVE_REASONS)


def _event_time(event: Mapping[str, Any]) -> Optional[datetime]:
    try:
        return as_utc(event["at"])
    except (KeyError, TypeError, ValueError):
        return None


def _within(event: Mapping[str, Any], now: datetime) -> bool:
    at = _event_time(event)
    return at is not None and as_utc(now) - at <= REVIVE_WINDOW


# ------------------------------------------------------------------------------------------------ the interval

def review_interval(config: Optional[UserBotConfig], defaults: Optional[UserBotConfig]) -> int:
    """Days between reviews: the user's setting, else the account default, else the system default. A value below 1 is no setting."""
    for source in (config, defaults):
        days = source.fact_review_interval_days if source is not None else None
        if days is not None and days >= 1:
            return days
    return DEFAULT_FACT_REVIEW_INTERVAL_DAYS


def interval_elapsed(last_review_at: Optional[datetime], now: datetime, tz: str, interval: int) -> bool:
    """`interval` owner-local calendar days passed since the last review; no review yet = elapsed. Counted in local
    dates, so a daily review is never skipped because yesterday's ran a few minutes later in the day."""
    if last_review_at is None:
        return True
    return (local_date(now, tz) - local_date(last_review_at, tz)).days >= interval


def digest_due(state: FactReviewState, now: datetime) -> bool:
    """Something was set aside since the last digest and the last one is DIGEST_INTERVAL old (or there is none)."""
    if state.undigested_since is None:
        return False
    return state.last_digest_at is None or now - state.last_digest_at >= DIGEST_INTERVAL


def review_due(
    stale_after_run: int,
    state: FactReviewState,
    questions: Sequence[FactReviewQuestion],
    interval: int,
    now: datetime,
    tz: str,
) -> Optional[ReviewReason]:
    """Why the janitor should enqueue a review for the account now, or None. The first that holds:
    `stale` (stale facts after this run and the interval elapsed), `reply` (a recorded reply nobody read),
    `timeout` (a question past its timeout), `digest` (due), `queue` (a queued question and no question that is out
    today to wait for)."""
    if stale_after_run > 0 and interval_elapsed(state.last_review_at, now, tz, interval):
        return "stale"
    if any(q.reply_not_shown for q in questions):
        return "reply"
    if any(q.is_timed_out(now) for q in questions):
        return "timeout"
    if digest_due(state, now):
        return "digest"
    if any(q.status is QuestionStatus.QUEUED for q in questions) and _slot_free(state, questions, local_date(now, tz), tz):
        return "queue"
    return None


def _slot_free(state: FactReviewState, questions: Sequence[FactReviewQuestion], today: date, tz: str) -> bool:
    """No current question is open, or the one that is was sent on an earlier day (it becomes unanswered at the next run)."""
    current = next((q for q in questions if q.question_id == state.current_question_id and q.is_open), None)
    return current is None or becomes_unanswered(current, today, tz)


# ------------------------------------------------------------------------------------------------ the pool and the queue alert

def pool(stale_count: int, questions: Iterable[FactReviewQuestion]) -> int:
    """Facts in the review's hands: the stale ones plus the facts of open questions."""
    return stale_count + len(_open_fact_ids(questions))


def queue_alert(state: FactReviewState, queued_count: int) -> Optional[QueueAlertAction]:
    """One alert when the queue passes QUEUE_ALERT (`raise`: record it in `queue_alert_at`), and nothing more until it
    has fallen to QUEUE_ALERT or fewer (`rearm`: clear the record)."""
    if queued_count > QUEUE_ALERT and state.queue_alert_at is None:
        return "raise"
    if queued_count <= QUEUE_ALERT and state.queue_alert_at is not None:
        return "rearm"
    return None


# ------------------------------------------------------------------------------------------------ basis and ask refusals

REVIVE_NEEDS_BASIS: Final[str] = "nothing here says it still holds: ask the owner instead"
_HISTORY_ONLY_WITH_ARCHIVE: Final[str] = "history goes only with an archive on basis {kind: reply} where the owner said it ended"
_BASIS_SHAPES: Final[Mapping[str, str]] = {
    "revive": "{kind: evidence, evidence_fact_id} or {kind: reply, question_id, quote}",
    "rewrite": "{kind: evidence, evidence_fact_id} or {kind: reply, question_id, quote}",
    "invalidate": "{kind: evidence, evidence_fact_id} or {kind: reply, question_id, quote}",
    "archive": "{kind: evidence, evidence_fact_id}, {kind: reply, question_id, quote} or {kind: date_passed, date}",
}


def is_dated_plan(fact: FactEntity, question: Optional[FactReviewQuestion] = None) -> bool:
    """A plan with a date: a `date` fact, or one whose question falls back to archive. When it is over it is archived
    only, with no history fact (Q8)."""
    return as_expiry_kind(fact.expiry_kind) is ExpiryKind.DATE or (question is not None and question.fallback == "archive")


def basis_refusal(
    action: ReviewAction,
    fact: FactEntity,
    basis: Optional[ReviewBasis],
    *,
    evidence: Optional[FactEntity],
    question: Optional[FactReviewQuestion],
    made_current: AbstractSet[str],
    history: Optional[str],
    today: date,
    tz: str,
) -> Optional[str]:
    """Why a review change has no reason behind it, as the text the model reads, or None when it has.

    `evidence`: the fact `basis.evidence_fact_id` names (for a supersede, the successor), as read, or None when there is
    none. `question`: the batch question `basis.question_id` names, or None. `made_current`: ids this pass made current.
    `history`: the text of the history fact sent with an archive. A fact comes back only on another fact or the owner's
    words; a plan is archived on its passed date; supersede names its successor, not a basis."""
    if history is not None and action != "archive":
        return _HISTORY_ONLY_WITH_ARCHIVE
    if action == "supersede":
        return _usable_fact_refusal("superseded_by", evidence, fact, made_current)
    if basis is None:
        return REVIVE_NEEDS_BASIS if action == "revive" else f"basis: {action} needs a basis: {_BASIS_SHAPES[action]}"
    if basis.kind == "date_passed" and action != "archive":
        return f"basis.kind date_passed backs only an archive; {action} needs {_BASIS_SHAPES[action]}"
    refusal = (
        _evidence_refusal(fact, basis, evidence, made_current) if basis.kind == "evidence"
        else _reply_refusal(fact, basis, question) if basis.kind == "reply"
        else _date_passed_refusal(fact, basis, today, tz)
    )
    return refusal or _history_refusal(action, fact, basis, question, history)


def _usable_fact_refusal(place: str, other: Optional[FactEntity], fact: FactEntity, made_current: AbstractSet[str]) -> Optional[str]:
    if other is None:
        return f"{place} names no fact of this account that is current"
    if other.account_id != fact.account_id:
        return f"{place} names no fact of this account that is current"
    if other.lineage_id == fact.lineage_id:
        return f"{place} is the fact's own lineage; another fact must say it"
    if other.state is not FactState.CURRENT and other.id not in made_current:
        return f"{place} names a fact that is {other.state.value}, not current"
    return None


def _evidence_refusal(
    fact: FactEntity, basis: ReviewBasis, evidence: Optional[FactEntity], made_current: AbstractSet[str]
) -> Optional[str]:
    if not basis.evidence_fact_id:
        return "basis.evidence_fact_id: the id of the current fact that says it"
    return _usable_fact_refusal("basis.evidence_fact_id", evidence, fact, made_current)


def _reply_refusal(fact: FactEntity, basis: ReviewBasis, question: Optional[FactReviewQuestion]) -> Optional[str]:
    if not basis.question_id or question is None or question.fact_id != fact.id:
        return "basis.question_id: the id of the question about this fact in the batch"
    if not question.reply_text:
        return "basis.question_id names a question with no recorded reply from the owner"
    quote = _squash(basis.quote or "")
    if not quote:
        return "basis.quote: words copied from the owner's reply"
    if quote not in _squash(question.reply_text):
        return "basis.quote is not in the owner's reply; copy words from it"
    return None


def _date_passed_refusal(fact: FactEntity, basis: ReviewBasis, today: date, tz: str) -> Optional[str]:
    if as_expiry_kind(fact.expiry_kind) is ExpiryKind.DATE:
        view, _ = expiry_view(fact.expiry_kind, fact.ttl_days, fact.expires_at, tz)
        own = date.fromisoformat(view["date"]) if view is not None and "date" in view else None
    else:
        own = basis.on
        if own is None:
            return "basis.date: the plan's date (YYYY-MM-DD), which must be before today"
    if own is None or own >= today:
        return f"basis.date_passed: the plan's date {own.isoformat() if own else 'is unknown'} is not before today ({today.isoformat()})"
    return None


def _history_refusal(
    action: ReviewAction, fact: FactEntity, basis: ReviewBasis, question: Optional[FactReviewQuestion], history: Optional[str]
) -> Optional[str]:
    if history is None:
        return None
    if action != "archive" or basis.kind != "reply":
        return _HISTORY_ONLY_WITH_ARCHIVE
    if is_dated_plan(fact, question):
        return "history: a plan that is over is archived only; leave history out"
    if not history.strip():
        return "history: one sentence in the past tense saying it ended"
    return None


def _squash(text: str) -> str:
    return " ".join(text.casefold().split())


def ask_refusal(
    fact: FactEntity,
    fallback: str,
    fallback_date: Optional[date],
    noise: bool,
    open_question: Optional[FactReviewQuestion],
    now: datetime,
    tz: str,
) -> Optional[str]:
    """Why the owner may not be asked about this fact now, as the text the model reads, or None.

    `open_question`: the fact's open question, if any. One that has timed out is a candidate sent back to the model once;
    its fallback applies, so it is not asked again. `archive` is the fallback only of a `date` fact, one whose
    `fallback_date` is before today, or noise; `fallback_date` and `noise` belong to archive."""
    if open_question is not None:
        if open_question.is_timed_out(now):
            return "ask_owner: this fact's question timed out and its fallback applies; decide it or leave it"
        return "ask_owner: this fact already has an open question; leave it"
    if fallback not in ("revive", "archive"):
        return "ask_owner.fallback: revive or archive"
    if fallback == "revive":
        if fallback_date is not None or noise:
            return "ask_owner: fallback_date and noise go with fallback archive only"
        return None
    today = local_date(now, tz)
    if fallback_date is not None and fallback_date >= today:
        return f"ask_owner.fallback_date: the plan's date must be before today ({today.isoformat()})"
    if as_expiry_kind(fact.expiry_kind) is ExpiryKind.DATE or fallback_date is not None or noise:
        return None
    return "ask_owner.fallback archive: only for a dated plan, with a past fallback_date, or with noise; use revive"
