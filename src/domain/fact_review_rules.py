"""The memory review's rules (FACT_LIFECYCLE_RFC D6/D7, Facts S4 plan §3.2, §3.7, §3.8, §3.11, §3.12, §3.14): which stale facts
go to the model, how the question queue is ordered and when a question may go out, when a question times out and what its
fact becomes then, and when the janitor must start a review. Pure: the services read and write, this decides.

Statuses of a question move only through the methods of `FactReviewQuestion`; nothing here builds a question by hand."""
from datetime import date, datetime, timedelta, timezone
from typing import AbstractSet, Any, Callable, Dict, Final, Iterable, List, Literal, Mapping, Optional, Sequence, cast

from .channel_binding import ChannelBinding
from .entities import ExpiryKind, FactEntity, FactState
from .fact_expiry import EXPIRY_NONE, Expiry, stored_expiry
from .fact_update import AS_HISTORY_SAME_TEXT, ParsedFactUpdate
from .fact_update_outcome import EXPIRY_REFUSALS, UpdateRefusal
from .fact_update_plan import content_changed
from .fact_lifecycle import (
    compute_expiry, expiry_pinned, horizon_from_now, lifecycle_event, lifecycle_fields, resolves_too_soon, revival_fields,
    with_event,
)
from .fact_review_basis import ReviewAction, ReviewBasis
from .fact_review_outcome import MADE_CURRENT, ReviewDecision
from .fact_review_question import (
    DIGEST_QUESTION_DAYS, TEXT_KEYS, AnswerSource, FactReviewQuestion, QuestionDelivery, QuestionFallback, QuestionKind,
    QuestionOrigin, QuestionStatus,
)
from .fact_review_reasons import (
    FallbackActor, fallback_reason, gives_window_reason, is_revive_reason, kept_twice_reason, queued_reason,
)
from .fact_review_state import FactReviewState
from .fact_review_task import neighbours_of
from .fact_words import written_text_problem
from .fact_write_context import as_utc
from .local_time import local_date
from .notification import NotificationChannel
from .pass_actor import PassActor
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
# The `expires_at` of a fact with none, in the batch order: after every dated one.
_NO_EXPIRY_KEY: Final[datetime] = datetime.max.replace(tzinfo=timezone.utc)
_DAY = timedelta(days=1)

# Why the janitor starts a review, in the order they are tried.
ReviewReason = Literal["stale", "reply", "timeout", "pending", "digest", "queue"]
QueueAlertAction = Literal["raise", "rearm"]
# Why no question may go to the owner's chat (S4-D21).
ChannelRefusal = Literal["no_primary_channel", "companion_channel"]


# ------------------------------------------------------------------------------------------------ selection

def queue_key(fact: FactEntity) -> datetime:
    """Where a fact stands in line: how long it has been stale (oldest first); no date means the front."""
    return as_utc(fact.stale_since) if fact.stale_since is not None else EARLIEST_QUEUE_KEY


def stale_order_key(fact: FactEntity) -> tuple[datetime, str]:
    """The order of a stale list: `queue_key` (longest stale first, no `stale_since` before every dated one), then the id."""
    return queue_key(fact), fact.id


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
    stale first, earliest `expires_at` with none last, id): a fact with an open question is the question's business, and
    one offered MAX_OFFERS times is asked instead (`kept_twice`). Parts of one plan, staled in one janitor run and ending
    on one day, so come in one batch (P2-2)."""
    held = open_fact_ids(questions)
    waiting = sorted(
        (f for f in stale if f.id not in held and offers.get(f.id, 0) < MAX_OFFERS),
        key=lambda f: (
            offers.get(f.id, 0), queue_key(f), as_utc(f.expires_at) if f.expires_at is not None else _NO_EXPIRY_KEY, f.id,
        ),
    )
    chosen: Dict[str, FactEntity] = {}
    for fact in (*reply_facts, *candidates, *waiting):
        chosen.setdefault(fact.id, fact)
    return list(chosen.values())[:REVIEW_BATCH]


def unread_replies(questions: Iterable[FactReviewQuestion]) -> List[FactReviewQuestion]:
    """The open questions with an answer in words the review has not read (`reply_not_shown`), in queue order: their
    facts go to the model first."""
    return queue_order(q for q in questions if q.reply_not_shown)


def offered(offers: Mapping[str, int], batch: Sequence[FactEntity]) -> Dict[str, int]:
    """`offers` once `batch` goes to the model: one more for each stale batch fact. A current batch fact is there for its
    question (a reply, a timeout), which is not an offer."""
    counted = dict(offers)
    for fact in batch:
        if fact.state is FactState.STALE:
            counted[fact.id] = counted.get(fact.id, 0) + 1
    return counted


def kept_twice(stale: Sequence[FactEntity], offers: Mapping[str, int], questions: Iterable[FactReviewQuestion]) -> List[FactEntity]:
    """The stale facts the model has left undecided MAX_OFFERS times and that have no open question: they are asked
    (never revived by code), the longest stale first."""
    held = open_fact_ids(questions)
    return sorted(
        (f for f in stale if offers.get(f.id, 0) >= MAX_OFFERS and f.id not in held), key=lambda f: (queue_key(f), f.id)
    )


def open_fact_ids(questions: Iterable[FactReviewQuestion]) -> frozenset[str]:
    """The facts of the open chat questions (queued, pending, asked, unanswered): the review holds them, the janitor skips
    them. A digest question's fact is already history (current, `none`): it holds nothing."""
    return frozenset(q.fact_id for q in questions if q.is_open and q.in_chat)


# ------------------------------------------------------------------------------------------------ the queue

def queue_order(questions: Iterable[FactReviewQuestion]) -> List[FactReviewQuestion]:
    """The order questions go out: the longest-stale fact first (`queue_key`), then the earliest queued, then the id."""
    return sorted(questions, key=lambda q: (q.queue_key, q.queued_at, q.question_id))


def next_in_queue(questions: Iterable[FactReviewQuestion]) -> Optional[FactReviewQuestion]:
    """The first queued chat question in queue order, or None. A digest question is never sent."""
    return next(iter(queue_order(q for q in questions if q.status is QuestionStatus.QUEUED and q.in_chat)), None)


def sent_today(state: FactReviewState, today: date) -> int:
    """Questions sent on the owner's local day `today`; the count of an earlier day is not today's."""
    return state.sent_today(today)


def current_question(state: FactReviewState, questions: Iterable[FactReviewQuestion]) -> Optional[FactReviewQuestion]:
    """The question the state points at, when it can be the one out: being sent (`pending`) or sent (`asked`). A pointer
    to anything else (closed, `unanswered`, missing) holds nothing."""
    return next(
        (
            q for q in questions
            if q.question_id == state.current_question_id and q.status in (QuestionStatus.PENDING, QuestionStatus.ASKED)
        ),
        None,
    )


def may_send(state: FactReviewState, today: date, current: Optional[FactReviewQuestion]) -> bool:
    """Whether the next queued question may go out on the local day `today`; `current` comes from `current_question`.

    Not while one is out, not after MAX_QUESTIONS_PER_DAY today, not on the day a delivery failed."""
    if current is not None:
        return False
    if sent_today(state, today) >= MAX_QUESTIONS_PER_DAY:
        return False
    return state.delivery_failed_on != today


def channel_refusal(
    primary: Optional[NotificationChannel], binding: Optional[ChannelBinding],
) -> Optional[ChannelRefusal]:
    """Why no question may be sent now, or None (S4-D21): questions go only to the owner's primary channel (`primary`;
    no fallback to the last active one), and never to one whose binding has a companion's memory (`binding`, that
    channel's), whose session is the companion's, not Alek's. Without a channel the queue waits; the Cabinet can
    answer it."""
    if primary is None:
        return "no_primary_channel"
    if binding is not None and binding.companion_config is not None:
        return "companion_channel"
    return None


def becomes_unanswered(question: FactReviewQuestion, now: datetime, tz: str) -> bool:
    """An `asked` question sent on an earlier owner-local day is `unanswered`: still open and timing out, no longer
    current. A recorded reply in words does not keep it current (the review reads it once; undecided, it waits for the
    timeout)."""
    return (
        question.status is QuestionStatus.ASKED
        and question.asked_at is not None
        and local_date(question.asked_at, tz) < local_date(now, tz)
    )


def stuck_pending(question: FactReviewQuestion, state: FactReviewState, now: datetime, tz: str) -> bool:
    """A `pending` question whose send is over: it is not the state's current one, or the day it was claimed (`sent_on`)
    is an earlier owner-local day. A send claimed today may still be running (`mode="next"` holds no lease), so it is
    left alone; a crash costs one day. Housekeeping completes it (it has a message ref) or retracts it (it has none)."""
    return question.status is QuestionStatus.PENDING and (
        state.current_question_id != question.question_id
        or state.sent_on is None
        or state.sent_on < local_date(now, tz)
    )


# ------------------------------------------------------------------------------------------------ timeouts

def due_timeouts(questions: Iterable[FactReviewQuestion], now: datetime) -> List[FactReviewQuestion]:
    """The questions whose QUESTION_TIMEOUT from `asked_at` has run out (asked and unanswered only), in queue order."""
    return queue_order(q for q in questions if q.is_timed_out(now))


def asked_fact_unchanged(question: FactReviewQuestion, fact: Optional[FactEntity]) -> bool:
    """Whether `fact`, the question's fact followed forward to the document that may be written now (None: gone, or
    closed or merged meanwhile), is still the very document the question asks about. A rewrite is a new document, so a
    fact rewritten or closed since it was queued has been settled elsewhere and its question no longer asks anything."""
    return fact is not None and fact.id == question.fact_id


def timeout_candidate(fact: FactEntity, question: FactReviewQuestion, nearest: Sequence[FactEntity]) -> bool:
    """S4-D19: a timed-out question's fact goes to the model once before any fallback when the owner answered meanwhile.
    First his own reply in words on the question, not yet read (`reply_not_shown`): an answer is not silence. Else one of
    its neighbours (`neighbours_of` over `nearest`, the current facts nearest to it by its vector, nearest first) created
    after the question was asked: what he said elsewhere may decide it. Otherwise silence decides (`fallback_fields`)."""
    if question.asked_at is None:
        return False
    if question.reply_not_shown:
        return True
    return any(as_utc(n.created_at) > question.asked_at for n in neighbours_of(fact, nearest))


def fallback_window_days(fact: FactEntity) -> int:
    """How long a silence brings a fact back for: twice the window it was last given, between MIN_FALLBACK_DAYS and
    MAX_FALLBACK_DAYS. The window runs from the later of `reported_date` and the fact's last revive or rewrite to
    `expires_at` (whole days), so every silence doubles what the last one gave (28, 56, ... days), though a horizon is
    stored from `reported_date`. A fact with no `expires_at` has its `ttl_days` as the window, else none (MIN).

    Exact only when the writer that gave the window stamped its trail entry with the same `now` it counted the horizon
    from, as `fallback_fields` does; a later stamp by a microsecond can cost a day."""
    if fact.expires_at is None:
        window = fact.ttl_days or 0
    else:
        window = (as_utc(fact.expires_at) - _last_given(fact)).days
    return min(MAX_FALLBACK_DAYS, max(MIN_FALLBACK_DAYS, 2 * window))


def keeps_expiry(fact: FactEntity, now: datetime) -> bool:
    """Whether a confirmation of the fact (the owner's yes, a silence, "I don't remember") leaves its own decision as it
    is: a pinned fact or one whose decision is `none` stays permanent, and a `date` still ahead stays that date unless the write path would refuse it as born due. Anything
    else (a horizon, a date that has passed, a legacy fact with no kind) is given the doubled window instead."""
    return _holds_no_expiry(fact) or _date_ahead(fact, now)


def confirmed_expiry(fact: FactEntity, now: datetime, tz: str) -> Expiry:
    """The decision a confirmation writes: `none` for a pinned fact or one that is permanent, the fact's own date while it
    is ahead (`stored_expiry`; `tz`: the owner's zone), else a horizon of `fallback_window_days`. A date inside its last
    day (`resolves_too_soon`, the write path's bound) is not kept: the write would refuse it. `keeps_expiry` is the same
    decision as a yes or no."""
    if expiry_pinned(fact.domain, fact.type, fact.tags):
        return EXPIRY_NONE
    if keeps_expiry(fact, now):
        kept = stored_expiry(fact.expiry_kind, fact.ttl_days, fact.expires_at, tz)
        if kept is not None:  # always: a permanent fact and a date with its moment both have one
            return kept
    return Expiry(ExpiryKind.HORIZON, days=fallback_window_days(fact))


def _holds_no_expiry(fact: FactEntity) -> bool:
    return expiry_pinned(fact.domain, fact.type, fact.tags) or fact.expiry_kind is ExpiryKind.NONE


def _date_ahead(fact: FactEntity, now: datetime) -> bool:
    return (
        fact.expiry_kind is ExpiryKind.DATE
        and fact.expires_at is not None
        and not resolves_too_soon(fact.expires_at, now)
    )


def _last_given(fact: FactEntity) -> datetime:
    given = (t for e in fact.lifecycle_events if _leads_to_current(e, gives_window_reason) and (t := _event_time(e)) is not None)
    return max([as_utc(fact.reported_date), *given])


def fallback_revives(question: FactReviewQuestion) -> bool:
    """Whether the question's fallback brings its fact back to current (`revive`)."""
    return question.fallback == "revive"


def fallback_sets_aside(question: FactReviewQuestion) -> bool:
    """Whether the question's fallback sets its fact aside (`archive`)."""
    return question.fallback == "archive"


def fallback_writes_history(question: FactReviewQuestion) -> bool:
    """Whether the question's fallback writes its `unknown` text as history (`history`, a typed rewrite)."""
    return question.fallback == "history"


def fallback_fields(fact: FactEntity, question: FactReviewQuestion, now: datetime, actor: FallbackActor) -> Dict[str, Any]:
    """The in-place write of a question's fallback, applied when nobody answered or the owner does not remember.

    `archive`: the fact is archived, its `stale_since` kept. `revive`: current again, `stale_since` cleared, a horizon of
    `fallback_window_days` counted from now and stored from `reported_date` (which stays); a fact whose decision
    `keeps_expiry` keeps it, so the expiry fields are left alone (a pinned fact that had a horizon gets `none`). Either way the trail
    gets one entry under `actor`, with a code-built reason. A `history` fallback is a new version, not an in-place write:
    ValueError (`fact_review_answers.history_fallback` builds it)."""
    if fallback_writes_history(question):
        raise ValueError("a history fallback is written through the typed path (history_fallback), not in place")
    if fact.state not in (FactState.STALE, FactState.CURRENT):
        raise ValueError(f"a fallback applies to a stale or current fact, not a {fact.state.value} one")
    reason = fallback_reason(question.question_id, question.fallback, actor)
    if fallback_revives(question):
        expiry: Dict[str, Any] = {}
        if not keeps_expiry(fact, now):
            expiry = horizon_from_now(fallback_window_days(fact), fact.reported_date, now)
        elif expiry_pinned(fact.domain, fact.type, fact.tags) and fact.expiry_kind is not ExpiryKind.NONE:
            expiry = compute_expiry(EXPIRY_NONE, fact.reported_date, "UTC")  # a pinned fact is permanent, as a yes says
        return revival_fields(fact, expiry, now, actor, reason)
    fields: Dict[str, Any] = lifecycle_fields(FactState.ARCHIVED, now)
    fields["last_updated"] = now
    event = lifecycle_event(now, fact.state, FactState.ARCHIVED, actor, reason)
    fields["lifecycle_events"] = with_event(fact.lifecycle_events, event)
    return fields


# ------------------------------------------------------------------------------------------------ revive alert

def revives_in_window(events: Sequence[Mapping[str, Any]], now: datetime) -> int:
    """How many times a fact came back to current within REVIVE_WINDOW of `now`, read from its `lifecycle_events`.

    A revive is an entry into `current` whose code-built reason names one (a review revive, an owner's yes, a
    fallback); queueing a fact for the owner is not one. An entry that cannot be read counts for nothing."""
    return sum(1 for e in events if _leads_to_current(e, is_revive_reason) and _within(e, now))


def revive_alert_due(events: Sequence[Mapping[str, Any]], now: datetime) -> bool:
    """A fact revived REVIVE_ALERT times or more in the window: its horizon is probably wrong."""
    return revives_in_window(events, now) >= REVIVE_ALERT


def _leads_to_current(event: Mapping[str, Any], reason_test: Callable[[object], bool]) -> bool:
    return event.get("to") == FactState.CURRENT.value and reason_test(event.get("reason"))


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


def iso_week_of(day: date) -> str:
    """The ISO week of a calendar day as `YYYY-Www` (the ISO year, which differs from the calendar year around 1 January):
    the weekly digest goes out once per such week. Raises ValueError for a datetime, which is not a day."""
    if isinstance(day, datetime):
        raise ValueError("day must be a date, not a datetime")
    year, week, _ = day.isocalendar()
    return f"{year:04d}-W{week:02d}"


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
    `timeout` (a question past its timeout), `pending` (a send that never finished, `stuck_pending`), `digest` (due),
    `queue` (a queued question that may go out: no question out since today, under the day's cap, no failed delivery
    today)."""
    if stale_after_run > 0 and interval_elapsed(state.last_review_at, now, tz, interval):
        return "stale"
    if any(q.reply_not_shown for q in questions):
        return "reply"
    if any(q.is_timed_out(now) for q in questions):
        return "timeout"
    if any(stuck_pending(q, state, now, tz) for q in questions):
        return "pending"
    if digest_due(state, now):
        return "digest"
    if any(q.status is QuestionStatus.QUEUED and q.in_chat for q in questions) and _queue_may_go(state, questions, now, tz):
        return "queue"
    return None


def _queue_may_go(state: FactReviewState, questions: Sequence[FactReviewQuestion], now: datetime, tz: str) -> bool:
    """A queued question may be sent at the next run: the current one, if any, was sent on an earlier day (it becomes
    unanswered there), and the day's cap and a failed delivery allow it."""
    current = current_question(state, questions)
    if current is not None and not becomes_unanswered(current, now, tz):
        return False
    return may_send(state, local_date(now, tz), None)


# ------------------------------------------------------------------------------------------------ the pool and the queue alert

def pool(stale_count: int, questions: Iterable[FactReviewQuestion]) -> int:
    """Facts in the review's hands: the stale ones plus the facts of open questions."""
    return stale_count + len(open_fact_ids(questions))


def queue_alert(state: FactReviewState, questions: Iterable[FactReviewQuestion]) -> Optional[QueueAlertAction]:
    """One alert when the queued questions pass QUEUE_ALERT (`raise`: record it in `queue_alert_at`), and nothing more
    until they are QUEUE_ALERT or fewer again (`rearm`: clear the record). Digest questions are not in the queue."""
    queued = sum(1 for q in questions if q.status is QuestionStatus.QUEUED and q.in_chat)
    if queued > QUEUE_ALERT and state.queue_alert_at is None:
        return "raise"
    if queued <= QUEUE_ALERT and state.queue_alert_at is not None:
        return "rearm"
    return None


# ------------------------------------------------------------------------------------------------ a question about a fact

def kept_twice_fallback(fact: FactEntity) -> QuestionFallback:
    """What silence does to a fact the review asked about by itself: a dated plan becomes history with its `unknown`
    text (P2-3), any other fact is kept (revive)."""
    return "history" if fact.expiry_kind is ExpiryKind.DATE else "revive"


def queued_question(
    fact: FactEntity,
    question_id: str,
    user_id: str,
    fallback: QuestionFallback,
    *,
    at: datetime,
    fallback_date: Optional[date] = None,
    noise: bool = False,
    question: QuestionKind = "still_true",
    texts: Optional[Mapping[str, str]] = None,
    origin: QuestionOrigin = "review",
    delivery: QuestionDelivery = "chat",
) -> FactReviewQuestion:
    """A new `queued` question about `fact`: it quotes the fact's text now and takes its place in line from the fact's
    `stale_since` (`queue_key`). The one construction for an ask (the review) and a kept-twice question (code); `question`,
    `texts`, `origin` and `delivery` are the record's (P2-3)."""
    return FactReviewQuestion(
        question_id=question_id,
        account_id=fact.account_id,
        user_id=user_id,
        fact_id=fact.id,
        quoted_text=fact.text,
        fallback=fallback,
        fallback_date=fallback_date,
        noise=noise,
        queue_key=queue_key(fact),
        queued_at=at,
        question=question,
        texts=dict(texts or {}),
        origin=origin,
        delivery=delivery,
    )


def kept_twice_question(fact: FactEntity, question_id: str, user_id: str, *, at: datetime) -> FactReviewQuestion:
    """The question code asks about a fact the review kept MAX_OFFERS times: a `date` fact is asked `happened` with the
    `history` fallback, any other `still_true` with `revive`; origin `kept_twice`, so its texts are built by code."""
    fallback = kept_twice_fallback(fact)
    kind: QuestionKind = "happened" if fallback == "history" else "still_true"
    return queued_question(fact, question_id, user_id, fallback, at=at, question=kind, origin="kept_twice")


def digest_question(
    fact: FactEntity, question_id: str, user_id: str, paid: str, not_paid: str, *, at: datetime
) -> FactReviewQuestion:
    """The Cabinet-only question about a routine invoice the review rewrote "payment not confirmed" (P2-2): kind `paid`,
    delivery `digest`, the `payment` texts, fallback `history` (never applied: it is never sent, so it never times out).
    Its place in line is its creation: a fresh history version has no `stale_since`."""
    return FactReviewQuestion(
        question_id=question_id,
        account_id=fact.account_id,
        user_id=user_id,
        fact_id=fact.id,
        quoted_text=fact.text,
        fallback="history",
        queue_key=at,
        queued_at=at,
        question="paid",
        texts={"paid": paid, "not_paid": not_paid},
        origin="review",
        delivery="digest",
    )


def digest_question_expired(question: FactReviewQuestion, now: datetime) -> bool:
    """A digest question unanswered DIGEST_QUESTION_DAYS after it was queued: it closes, its fact already says "payment
    not confirmed"."""
    return not question.in_chat and question.queued_at + DIGEST_QUESTION_DAYS <= now


# The stored state an ask queues a fact from: a fact that is no longer stale was decided elsewhere meanwhile.
QUEUE_ACCEPTS: Final[frozenset[FactState]] = frozenset({FactState.STALE})


def queued_fields(fact: FactEntity, question_id: str, now: datetime) -> Dict[str, Any]:
    """The in-place write that makes a stale fact current while its question waits (plan §3.2 step 11): `stale_since`
    cleared, the expiry decision as stored, and one trail entry under the review with `queued_reason`, which is no revive.
    Build the question (`queued_question`) from the fact as read before this write: its place in line is the fact's
    `stale_since`. Raises ValueError for a fact that is not stale."""
    return _queued_for_owner(fact, now, PassActor.REVIEW.value, queued_reason(question_id))


# The trail actor of a fact queued by code because the review left it undecided MAX_OFFERS times (plan §2.1).
KEPT_TWICE_ACTOR: Final[str] = "review_fallback"


def kept_twice_fields(fact: FactEntity, question_id: str, now: datetime) -> Dict[str, Any]:
    """`queued_fields` for a fact the review kept MAX_OFFERS times (`kept_twice`) and code queues itself: the same write,
    with its trail entry under KEPT_TWICE_ACTOR and `kept_twice_reason`, which is no revive. Its question is
    `queued_question` with `kept_twice_fallback`. Raises ValueError for a fact that is not stale."""
    return _queued_for_owner(fact, now, KEPT_TWICE_ACTOR, kept_twice_reason(question_id))


def _queued_for_owner(fact: FactEntity, now: datetime, actor: str, reason: str) -> Dict[str, Any]:
    if fact.state is not FactState.STALE:
        raise ValueError(f"a fact is queued for the owner from stale, not from {fact.state.value}")
    return revival_fields(fact, {}, now, actor, reason)


def review_close_source(question: FactReviewQuestion) -> Optional[AnswerSource]:
    """The answer source of a question the review closes: `review` when it decided on the owner's words (a recorded
    reply), else none."""
    return "review" if question.reply_text else None


# ------------------------------------------------------------------------------------------------ basis and ask refusals

REVIVE_NEEDS_BASIS: Final[str] = "nothing here says it still holds: ask the owner instead"
_HISTORY_ONLY_WITH_ARCHIVE: Final[str] = "history goes only with an archive on basis {kind: reply} where the owner said it ended"
_BASIS_SHAPES: Final[Mapping[str, str]] = {
    "revive": "{kind: evidence, evidence_fact_id} or {kind: reply, question_id, quote}",
    "rewrite": "{kind: evidence, evidence_fact_id} or {kind: reply, question_id, quote}",
    "invalidate": "{kind: evidence, evidence_fact_id} or {kind: reply, question_id, quote}",
    "archive": "{kind: reply, question_id, quote}",
    "history": (
        "{kind: evidence, evidence_fact_id}, {kind: reply, question_id, quote} or {kind: date_passed, date}"
    ),
}
# P2-4: the review archives only on the owner's words; a fact that is over becomes history.
ARCHIVE_NEEDS_REPLY: Final[str] = (
    "an archive needs the owner's reply ({kind: reply, question_id, quote}); a fact that is over is rewritten as history "
    "(as_history: true), or ask the owner with question happened"
)
DATE_PASSED_ONLY_HISTORY: Final[str] = "basis.kind date_passed backs only a history rewrite (as_history: true)"
HISTORY_WITH_REWRITE: Final[str] = (
    "history goes beside an archive; to rewrite this fact as history send as_history: true"
)
DATED_PLAN_HISTORY: Final[str] = (
    "history: a fact of expiry kind date is rewritten as history (as_history: true with content), not archived beside a "
    "history fact"
)
# A question that falls back to archive asks about noise (P2-4; a stored S4 one, about a plan set aside): when it is over it
# is archived, never history.
NOISE_ARCHIVED_ONLY: Final[str] = (
    "history: a fact whose question falls back to archive is noise and is archived only; leave history out"
)
NOT_EVIDENCE: Final[str] = "{place} names a fact this pass rewrote as history on date_passed; that is no evidence"
_PAYMENT_KEYS: Final[tuple[str, ...]] = ("paid", "not_paid")
# Why a fold may not join an ask (`fold_refusal`).
FoldRefusal = Literal["self", "not_in_batch", "taken"]


def archived_only(fact: FactEntity, question: FactReviewQuestion) -> bool:
    """Whether the owner's "No longer" archives the fact with no history fact beside it: a dated plan (`is_dated_plan`,
    an S4 question about one) or noise (the question falls back to archive, `fallback_sets_aside`). Only a standing fact
    gets its history (S4-D27)."""
    return is_dated_plan(fact) or fallback_sets_aside(question)


def is_dated_plan(fact: FactEntity) -> bool:
    """A plan with a date: a `date` fact. When it is over it becomes history (P2-1); an S4 "No longer" on it archives
    it only, with no history fact (Q8)."""
    return fact.expiry_kind is ExpiryKind.DATE


STAYS_STALE_REFUSAL: Final[str] = "this pass changes the fact's state; reported_date is kept by the system"

# The texts of the review pass's own scope (plan §3.5). `place` is the argument the refusal is about, as the model wrote it.
UPDATE_FACT_PLACE: Final[str] = "update_fact.fact_id"
ASK_OWNER_PLACE: Final[str] = "ask_owner.fact_id"
CHANGED_OUTSIDE: Final[str] = "this fact changed outside the review; leave it, it stays as it is"
UPDATES_SHAPE_REFUSAL: Final[str] = (
    "update_fact.updates must be an object of the fields to change: "
    "{content?, tags?, metadata?, temporal_class?, state?, superseded_by?, expiry?}"
)
ASK_HELD: Final[str] = (
    "held: code writes the question and sends it to the owner after this pass; this fact needs nothing more now"
)


def not_in_batch_refusal(place: str) -> str:
    return f"{place} names no fact of this batch: use the fact_id of a fact listed for this review"


def asked_refusal(place: str) -> str:
    return f"{place}: you asked the owner about this fact in this pass; it stays as it is until he answers"


def decided_refusal(place: str) -> str:
    return f"{place}: this fact was already decided in this pass; leave it"


def changed_outside_refusal(place: str) -> str:
    return f"{place}: {CHANGED_OUTSIDE}"


def not_under_review_refusal(place: str) -> str:
    return f"{place} names a current fact with no question in this batch; leave it, it stays as it is"


def result_refusal(place: str, *, asked: bool, decision: Optional[ReviewDecision], write: bool) -> Optional[str]:
    """One result per fact: an ask holds it; a write decides it, except that a write may follow one that left the lineage
    current (R3-13). `asked` and `decision` are the pass's result so far for the fact's lineage."""
    if asked:
        return asked_refusal(place)
    if decision is not None and not (write and decision.outcome in MADE_CURRENT):
        return decided_refusal(place)
    return None


def may_write_current(decision: Optional[ReviewDecision], has_open_question: bool) -> bool:
    """A current document of a batch lineage is the pass's to write when its fact has an open question or this pass made it
    current."""
    return (decision is not None and decision.outcome in MADE_CURRENT) or has_open_question


def made_current_documents(decisions: Mapping[str, ReviewDecision], left: Mapping[str, FactEntity]) -> AbstractSet[str]:
    """The documents this pass made current (they count as current evidence and successors, R3-13): per lineage in
    `decisions`, the document `left` holds. A history this pass rewrote on `date_passed` is left out: it is no evidence
    (`date_passed_histories`)."""
    return {
        left[lineage].id for lineage, decision in decisions.items()
        if decision.outcome in MADE_CURRENT and not _date_passed_history(decision)
    }


def date_passed_histories(decisions: Mapping[str, ReviewDecision], left: Mapping[str, FactEntity]) -> AbstractSet[str]:
    """The documents this pass rewrote as history on `date_passed` (P2-2): no evidence and no successor in this pass, though
    they are current; the next run reads them as ordinary current facts."""
    return {
        left[lineage].id for lineage, decision in decisions.items()
        if decision.outcome in MADE_CURRENT and _date_passed_history(decision)
    }


def _date_passed_history(decision: ReviewDecision) -> bool:
    return decision.as_history and decision.basis_kind == "date_passed"


def reason_plan_date(fact: FactEntity, basis: Optional[ReviewBasis], tz: str) -> Optional[date]:
    """The plan's date for the trail reason: only a `date_passed` basis has one."""
    return plan_date(fact, basis, tz) if basis is not None and basis.kind == "date_passed" else None


def needs_change_check(refusal: Optional[UpdateRefusal]) -> bool:
    """Whether a refused write is checked for the fact having changed outside the review (then it reads as that): an
    expiry refusal is about the update itself and is not."""
    return refusal not in EXPIRY_REFUSALS


def review_action(current: FactEntity, update: ParsedFactUpdate) -> Optional[ReviewAction]:
    """What a review write does to `current`, or None when it leaves the fact as it is (a stale fact stays stale).

    A closing state decides alone (`supersede`, `archive`, `invalidate`), whatever else the call carries. Otherwise a
    change of what the fact says (text, tags, metadata, temporal class: the version rule of `plan_fact_update`) is a
    `rewrite`, and a state `current`, or a new expiry on a fact that is already current, is a `revive`."""
    if update.state is FactState.SUPERSEDED:
        return "supersede"
    if update.state is FactState.ARCHIVED:
        return "archive"
    if update.state is FactState.INVALIDATED:
        return "invalidate"
    if content_changed(current, update):
        return "rewrite"
    if update.state is FactState.CURRENT or (update.expiry is not None and current.state is FactState.CURRENT):
        return "revive"
    return None


def basis_refusal(
    current: FactEntity,
    update: ParsedFactUpdate,
    basis: Optional[ReviewBasis],
    *,
    named_fact: Optional[FactEntity],
    question: Optional[FactReviewQuestion],
    made_current: AbstractSet[str],
    history: Optional[str],
    today: date,
    tz: str,
    subject: Optional[FactEntity] = None,
    not_evidence: AbstractSet[str] = frozenset(),
) -> Optional[str]:
    """Why a review change has no reason behind it, as the text the model reads, or None when it has.

    The action is `review_action(current, update)`; a call that changes nothing is refused with STAYS_STALE_REFUSAL.
    `named_fact`: the fact `basis.evidence_fact_id` names (for a supersede, `update.superseded_by`), as read, or None
    when there is none. `question`: the batch question `basis.question_id` names, or None. `made_current`: ids this pass
    made current. `not_evidence`: ids this pass rewrote as history on `date_passed` (`date_passed_histories`), which are
    neither evidence nor a successor. `history`: the text of the history fact sent with an archive. `subject`: the batch
    fact the pass is deciding (the document its question names), when `current` is a later version of it; default
    `current`.

    A fact comes back only on another fact or the owner's words; it is archived only on the owner's words (P2-4); a fact
    that is over is rewritten as history (`update.as_history`) on evidence, a reply or its passed date (`date_passed`
    backs nothing else); supersede names its successor, not a basis."""
    action = review_action(current, update)
    if action is None:
        return STAYS_STALE_REFUSAL
    fact = current
    evidence = named_fact
    if history is not None and action != "archive":
        return HISTORY_WITH_REWRITE if action == "rewrite" else _HISTORY_ONLY_WITH_ARCHIVE
    if update.as_history and (action != "rewrite" or (update.content or "").strip() == current.text.strip()):
        return AS_HISTORY_SAME_TEXT
    if action == "supersede":
        return _usable_fact_refusal("superseded_by", evidence, fact, made_current, not_evidence)
    if basis is None:
        if action == "revive":
            return REVIVE_NEEDS_BASIS
        if update.as_history:
            return f"basis: a history rewrite needs a basis: {_BASIS_SHAPES['history']}"
        return f"basis: {action} needs a basis: {_BASIS_SHAPES[action]}"
    if action == "archive" and basis.kind != "reply":
        return ARCHIVE_NEEDS_REPLY
    if basis.kind == "date_passed" and not update.as_history:
        return f"{DATE_PASSED_ONLY_HISTORY}; {action} needs {_BASIS_SHAPES[action]}"
    refusal = (
        _evidence_refusal(fact, basis, evidence, made_current, not_evidence) if basis.kind == "evidence"
        else _reply_refusal(subject or fact, basis, question) if basis.kind == "reply"
        else _date_passed_refusal(fact, basis, today, tz)
    )
    return refusal or _history_refusal(action, fact, basis, question, history)


def _usable_fact_refusal(
    place: str, other: Optional[FactEntity], fact: FactEntity, made_current: AbstractSet[str],
    not_evidence: AbstractSet[str] = frozenset(),
) -> Optional[str]:
    if other is None:
        return f"{place} names no fact of this account that is current"
    if other.account_id != fact.account_id:
        return f"{place} names no fact of this account that is current"
    if other.lineage_id == fact.lineage_id:
        return f"{place} is the fact's own lineage; another fact must say it"
    if other.id in not_evidence:
        return NOT_EVIDENCE.format(place=place)
    if other.state is not FactState.CURRENT and other.id not in made_current:
        return f"{place} names a fact that is {other.state.value}, not current"
    return None


def _evidence_refusal(
    fact: FactEntity, basis: ReviewBasis, evidence: Optional[FactEntity], made_current: AbstractSet[str],
    not_evidence: AbstractSet[str],
) -> Optional[str]:
    if not basis.evidence_fact_id:
        return "basis.evidence_fact_id: the id of the current fact that says it"
    return _usable_fact_refusal("basis.evidence_fact_id", evidence, fact, made_current, not_evidence)


def _reply_refusal(subject: FactEntity, basis: ReviewBasis, question: Optional[FactReviewQuestion]) -> Optional[str]:
    """`subject`: the batch fact the question is about, whichever version of its lineage is being written (R3-13)."""
    if (
        not basis.question_id or question is None or question.question_id != basis.question_id
        or question.fact_id != subject.id
    ):
        return "basis.question_id: the id of the question about this fact in the batch"
    if not question.reply_text:
        return "basis.question_id names a question with no recorded reply from the owner"
    quote = _squash(basis.quote or "")
    if not quote:
        return "basis.quote: words copied from the owner's reply"
    if quote not in _squash(question.reply_text):
        return "basis.quote is not in the owner's reply; copy words from it"
    return None


def plan_date(fact: FactEntity, basis: ReviewBasis, tz: str) -> Optional[date]:
    """The date a `date_passed` basis says has passed: a `date` fact's own (its last day in the owner's zone `tz`), else
    `basis.on`; None when there is none."""
    if fact.expiry_kind is ExpiryKind.DATE:
        kept = stored_expiry(fact.expiry_kind, fact.ttl_days, fact.expires_at, tz)
        return kept.on if kept is not None else None
    return basis.on


def _date_passed_refusal(fact: FactEntity, basis: ReviewBasis, today: date, tz: str) -> Optional[str]:
    own = plan_date(fact, basis, tz)
    if own is None and fact.expiry_kind is not ExpiryKind.DATE:
        return "basis.date: the plan's date (YYYY-MM-DD), which must be before today"
    if own is None or own >= today:
        return f"basis.date_passed: the plan's date {own.isoformat() if own else 'is unknown'} is not before today ({today.isoformat()})"
    return None


def _history_refusal(
    action: ReviewAction, fact: FactEntity, basis: ReviewBasis, question: Optional[FactReviewQuestion],
    history: Optional[str],
) -> Optional[str]:
    if history is None:
        return None
    if action != "archive" or basis.kind != "reply":
        return _HISTORY_ONLY_WITH_ARCHIVE
    if is_dated_plan(fact):
        return DATED_PLAN_HISTORY
    if question is not None and fallback_sets_aside(question):
        return NOISE_ARCHIVED_ONLY
    if not history.strip():
        return "history: one sentence in the past tense saying it ended"
    return None


def payment_refusal(update: ParsedFactUpdate, payment: Optional[Mapping[str, str]]) -> Optional[str]:
    """Why a write may not carry `payment` (the paid and not-paid texts of a routine invoice, P2-2), or None: only an
    `as_history` rewrite takes it, and each text is one the review may write (`written_text_problem`)."""
    if payment is None:
        return None
    if not update.as_history:
        return "payment goes only with an as_history rewrite of an invoice that was received"
    for key in _PAYMENT_KEYS:
        problem = written_text_problem(payment.get(key))
        if problem is not None:
            return f"payment.{key}: {problem}"
    return None


def fold_refusal(fold_id: str, why: FoldRefusal) -> str:
    """The text of a fold the ask may not take: the asked fact itself, no other batch fact, or one already decided,
    asked or with an open question in this pass."""
    if why == "self":
        return f"ask_owner.folds: {fold_id} is the asked fact itself"
    if why == "not_in_batch":
        return f"ask_owner.folds: {fold_id} names no other fact of this batch"
    return f"ask_owner.folds: {fold_id} already has a result, an ask or an open question in this pass"


def _squash(text: str) -> str:
    return " ".join(text.casefold().split())


_QUESTION_KINDS: Final[tuple[str, ...]] = ("still_true", "happened", "renewed", "paid")


def ask_refusal(
    fact: FactEntity,
    fallback: str,
    fallback_date: Optional[date],
    noise: bool,
    open_question: Optional[FactReviewQuestion],
    now: datetime,
    tz: str,
    *,
    question: str = "still_true",
    texts: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Why the owner may not be asked about this fact now, as the text the model reads, or None.

    `open_question`: the fact's open question, if any. One that has timed out is a candidate sent back to the model once;
    its fallback applies, so it is not asked again. P2-3: `question` is the kind asked and `texts` the outcome texts the
    review writes now, exactly the kind's (`TEXT_KEYS`), each one a text the review may write (`written_text_problem`).
    `revive` is the fallback of a `still_true` question; `history` of a `happened`, `renewed` or `paid` one; `archive` only
    with `noise`, never for `paid`. A `date` fact is asked `happened`, `renewed` or `paid`, or `still_true` as noise.
    `fallback_date` is retired. `tz` is the owner's zone (no rule reads it since `fallback_date` went)."""
    if open_question is not None:
        if open_question.is_timed_out(now):
            return "ask_owner: this fact's question timed out and its fallback applies; decide it or leave it"
        return "ask_owner: this fact already has an open question; leave it"
    refusal = _ask_shape_refusal(fact, fallback, fallback_date, noise, question)
    if refusal is not None:
        return refusal
    return _ask_texts_refusal(question, texts or {})


def _ask_shape_refusal(
    fact: FactEntity, fallback: str, fallback_date: Optional[date], noise: bool, question: str
) -> Optional[str]:
    if question not in _QUESTION_KINDS:
        return "ask_owner.question: still_true, happened, renewed or paid"
    if fallback not in ("revive", "archive", "history"):
        return "ask_owner.fallback: revive, archive or history"
    if fallback_date is not None:
        return (
            "ask_owner.fallback_date is retired: a plan whose date passed is asked with question happened and fallback "
            "history"
        )
    if noise and fallback != "archive":
        return "ask_owner: noise goes with fallback archive only"
    if fallback == "archive" and not noise:
        return (
            "ask_owner.fallback archive: only with noise: true; a plan whose date passed is asked with question happened "
            "and fallback history"
        )
    if fallback == "history" and question == "still_true":
        return "ask_owner.fallback history: only with question happened, renewed or paid"
    if fallback == "revive" and question in ("happened", "renewed"):
        return (
            "ask_owner.fallback revive: a happened or renewed question falls back to history (or archive with noise: "
            "true)"
        )
    if question == "paid" and fallback != "history":
        return "ask_owner.fallback: a paid question falls back to history"
    if fact.expiry_kind is ExpiryKind.DATE and question == "still_true" and fallback != "archive":
        return (
            "ask_owner: a fact of expiry kind date is asked with question happened, renewed or paid, or as still_true "
            "with fallback archive and noise: true"
        )
    return None


def _ask_texts_refusal(question: str, texts: Mapping[str, str]) -> Optional[str]:
    kind = cast(QuestionKind, question)  # checked against _QUESTION_KINDS before
    keys = TEXT_KEYS[(kind, "chat")]
    if set(texts) != set(keys):
        return f"ask_owner.texts: give exactly {', '.join(keys) or 'none'} for question {kind}"
    for key in keys:
        problem = written_text_problem(texts[key])
        if problem is not None:
            return f"ask_owner.texts.{key}: {problem}"
    return None
