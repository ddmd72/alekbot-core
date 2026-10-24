"""SCD2 flags that go with a fact state."""

from datetime import datetime, time, timedelta
from typing import AbstractSet, Any, Callable, Dict, Final, List, Mapping, Optional, Protocol, Sequence, Tuple, assert_never

from .entities import ExpiryKind, FactDomain, FactEntity, FactState, FactType
from .exceptions import FactBornDueError, FactExpiryError, FactResolutionError
from .fact_expiry import EXPIRY_NONE, NOTHING_WRITTEN, Expiry, as_expiry_kind
from .local_time import local_date, local_moment, zone_or_utc
from .refusal_wording import RefusalWording

LIFECYCLE_EVENTS_CAP: Final[int] = 20
_DAY = timedelta(days=1)
_CLEANUP_FLOOR = timedelta(days=2)

# is_current: latest document of its lineage (only a superseded document has a successor); valid_to: when this version stopped being current truth.
# state -> (is_current, closes_validity)
_LIFECYCLE: dict[FactState, tuple[bool, bool]] = {
    FactState.CURRENT: (True, False),
    FactState.STALE: (True, False),
    FactState.ARCHIVED: (True, True),
    FactState.INVALIDATED: (True, True),
    FactState.SUPERSEDED: (False, True),
}


def lifecycle_fields(state: FactState, now: datetime) -> dict[str, Any]:
    """state, is_current and valid_to for a fact moving to `state`."""
    try:
        is_current, closes = _LIFECYCLE[state]
    except KeyError:
        raise ValueError(f"No lifecycle mapping for fact state: {state!r}") from None
    return {"state": state, "is_current": is_current, "valid_to": now if closes else None}


CONSOLIDATION_ACCEPTS: frozenset[FactState] = frozenset({FactState.CURRENT})
# The review and the owner's answers also write a fact the janitor hid.
REVIEW_ACCEPTS: frozenset[FactState] = frozenset({FactState.STALE, FactState.CURRENT})
# A fact in one of these states carries no expiry decision of its own: a decision sent with it is ignored.
CLOSING_STATES: frozenset[FactState] = frozenset({FactState.ARCHIVED, FactState.INVALIDATED, FactState.SUPERSEDED})
# The owner may invalidate a fact the consolidator archived meanwhile.
CABINET_ACCEPTS: frozenset[FactState] = frozenset(FactState) - {FactState.SUPERSEDED}

# Everything else on a stored fact is written by a new version document.
IN_PLACE_FIELDS: frozenset[str] = frozenset(
    {
        "state", "is_current", "valid_to", "superseded_by", "reported_date", "last_updated",
        "expiry_kind", "ttl_days", "expires_at", "stale_since", "lifecycle_events",
    }
)

MAX_FORWARD_HOPS = 50


def successor_unknown(start_id: str) -> str:
    """The refusal when `start_id` was superseded and its successor cannot be read (none stored, or the document is gone)."""
    return f"fact {start_id} was superseded and its successor is unknown; search again"


def forward_step(
    fact: FactEntity,
    accepted: AbstractSet[FactState],
    origin_lineage_id: str,
    start_id: str,
    origin_account_id: str,
    hops: int,
) -> Optional[str]:
    """One step of following `start_id` to the fact a caller may write: None = `fact` is it, a str = read that id next.

    `hops` is the number of hops already made. Raises FactResolutionError when the chain cannot end in `accepted`
    or would need more than MAX_FORWARD_HOPS hops. The state is read before `superseded_by`.
    """
    if fact.account_id != origin_account_id:
        raise FactResolutionError(f"fact {start_id} cannot be resolved; search again")
    if fact.lineage_id != origin_lineage_id:
        raise FactResolutionError(f"fact {start_id} was merged into {fact.id}; search again", current_id=fact.id)
    if fact.state in accepted:
        return None
    if fact.state is FactState.SUPERSEDED:
        if fact.superseded_by is None:
            raise FactResolutionError(successor_unknown(start_id))
        if hops >= MAX_FORWARD_HOPS:
            raise FactResolutionError(f"fact {start_id} has more than {MAX_FORWARD_HOPS} forward hops; search again")
        return fact.superseded_by
    if fact.id == start_id:
        raise FactResolutionError(f"fact {start_id} is {fact.state.value}", current_id=fact.id)
    raise FactResolutionError(
        f"fact {start_id} was replaced by {fact.id}, which is {fact.state.value}", current_id=fact.id
    )


def closure_fields(successor_id: str, now: datetime) -> dict[str, Any]:
    """The in-place write that closes a fact in favour of `successor_id`."""
    return {**lifecycle_fields(FactState.SUPERSEDED, now), "superseded_by": successor_id, "last_updated": now}


def superseded_closure(stored_fact: FactEntity, new_fact: FactEntity, now: datetime, actor: str) -> dict[str, Any]:
    """The in-place write that closes `stored_fact` in favour of `new_fact`, with its trail entry.

    The reason is `merged into <id>` when the lineages differ, else `replaced by <id>`. `actor` is the trail actor as
    written (a pass label, or a non-pass writer).
    """
    merged = stored_fact.lineage_id != new_fact.lineage_id
    reason = f"{'merged into' if merged else 'replaced by'} {new_fact.id}"
    entry = lifecycle_event(now, stored_fact.state, FactState.SUPERSEDED, actor, reason)
    return {**closure_fields(new_fact.id, now), "lifecycle_events": with_event(stored_fact.lifecycle_events, entry)}


def cabinet_invalidation(shown_text: str, stored_fact: FactEntity, requested_id: str, now: datetime) -> dict[str, Any]:
    """The in-place write of the owner's Cabinet delete of `requested_id`, which resolved to `stored_fact`.

    A version is closed with its own text kept, so `shown_text` (the text of `requested_id`) is what the owner's page
    displayed. When the walk moved to a newer document with another text, that text was never seen: FactResolutionError
    (the page reloads) instead of deleting it (owner, 2026-10-23).
    """
    if stored_fact.id != requested_id and stored_fact.text != shown_text:
        raise FactResolutionError(
            f"fact {requested_id} was rewritten after the page loaded; reload", current_id=stored_fact.id,
        )
    event = lifecycle_event(now, stored_fact.state, FactState.INVALIDATED, "cabinet", "deleted in the Cabinet")
    return {
        **lifecycle_fields(FactState.INVALIDATED, now),
        "last_updated": now,
        "lifecycle_events": with_event(stored_fact.lifecycle_events, event),
    }


def compute_expiry(expiry: Expiry, reported_date: datetime, timezone: str) -> Dict[str, Any]:
    """`expiry_kind`, `ttl_days` and `expires_at` (aware UTC) for a decision.

    A date is due at the end of that day in `timezone` (the start of the next local day, DST-safe); a horizon counts from
    `reported_date`; none stores nulls.
    """
    if expiry.kind is ExpiryKind.DATE and expiry.on is not None:
        midnight = datetime.combine(expiry.on + _DAY, time.min, tzinfo=zone_or_utc(timezone))
        return {"expiry_kind": ExpiryKind.DATE, "ttl_days": None, "expires_at": local_moment(midnight, "UTC")}
    if expiry.kind is ExpiryKind.HORIZON and expiry.days is not None:
        return {
            "expiry_kind": ExpiryKind.HORIZON,
            "ttl_days": expiry.days,
            "expires_at": local_moment(reported_date, "UTC") + timedelta(days=expiry.days),
        }
    return {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}


def horizon_from_now(days: int, reported_date: datetime, now: datetime) -> Dict[str, Any]:
    """`expiry_kind`, `ttl_days` and `expires_at` for a horizon of `days` counted from the later of `reported_date` and `now`.

    Stored as every horizon is, `expires_at = reported_date + ttl_days` whole days, so it can fall up to a day after
    `base + days`. The review and the owner's answers bring a fact back this way: nobody spoke of it today, so counting
    from `reported_date` alone would be born due.
    """
    if days < 1:
        raise ValueError("a horizon from now is at least one day")
    reported = local_moment(reported_date, "UTC")
    target = max(reported, local_moment(now, "UTC")) + timedelta(days=days)
    ttl_days = -((reported - target) // _DAY)  # ceil((target - reported) / 1 day)
    return compute_expiry(Expiry(ExpiryKind.HORIZON, days=ttl_days), reported, "UTC")


def resolves_too_soon(expires_at: datetime, now: datetime) -> bool:
    """The one bound of "born due": an expiry that resolves at or before now + 1 day is refused on write (the day's margin
    covers the model answering later). `born_due` and the rules that keep a stored date read it here."""
    return local_moment(expires_at, "UTC") <= local_moment(now, "UTC") + _DAY


class DateRefusalRule(Protocol):
    """The part of a pass's row the born-due rule reads: whether a due horizon is floored, and which text a refusal gets.

    `PassActor` is one (its rows); the protocol lives here because `pass_actor` imports this module."""

    @property
    def floors_due_horizon(self) -> bool: ...

    @property
    def refusal_wording(self) -> RefusalWording: ...


# The review's way out of a born-due refusal (Facts S4 plan §3.6): no DISCARD, no candidate, no history rewrite.
_REVIEW_WAY_OUT: Final[str] = (
    "this pass reviews stored memory: give a horizon (counted from now), or archive with basis date_passed when its date "
    "is over; doing nothing keeps the fact hidden"
)


def born_due(
    fields: Mapping[str, Any], reported_date: datetime, now: datetime, timezone: str, rule: DateRefusalRule
) -> Tuple[Dict[str, Any], Optional[str]]:
    """No expiry is born due: `fields` (from compute_expiry) as they may be written, and an optional note for the model.

    A decision that `resolves_too_soon` (at or before now + 1 day) is refused with FactBornDueError, worded by
    `rule.refusal_wording`. The one exception is a horizon on a pass whose row `floors_due_horizon` (stage 2a/2b,
    backstop, cluster), which cannot move `reported_date`: it is floored to now + 2 days, so the fact reaches review soon.
    """
    kind, expires_at = as_expiry_kind(fields.get("expiry_kind")), fields.get("expires_at")
    if kind not in (ExpiryKind.DATE, ExpiryKind.HORIZON) or expires_at is None:
        return dict(fields), None
    now = local_moment(now, "UTC")
    expires = local_moment(expires_at, "UTC")
    if not resolves_too_soon(expires, now):
        return dict(fields), None
    if kind is ExpiryKind.DATE:
        raise _date_refusal(expires, reported_date, now, timezone, rule.refusal_wording)
    if rule.floors_due_horizon:
        review_on = now + _CLEANUP_FLOOR
        note = (
            f"counted from {local_date(reported_date, timezone).isoformat()} this horizon had already passed; "
            f"the fact goes to review on {local_date(review_on, timezone).isoformat()}"
        )
        return {**fields, "expires_at": review_on}, note
    raise _horizon_refusal(fields, expires, reported_date, now, timezone, rule.refusal_wording)


def _horizon_refusal(
    fields: Mapping[str, Any], expires: datetime, reported_date: datetime, now: datetime, timezone: str,
    wording: RefusalWording,
) -> FactBornDueError:
    ended = "ended" if expires <= now else "ends"
    expires_on = local_date(expires, timezone).isoformat()
    match wording:
        case RefusalWording.STAGE1:
            return FactBornDueError(
                f"counted from when the owner said it ({local_date(reported_date, timezone).isoformat()}), a horizon of "
                f"{fields.get('ttl_days')} days {ended} {expires_on}: "
                f"give at least {_min_horizon_days(reported_date, now)} days, rewrite the fact as history with {{kind: none}}, "
                f"or DISCARD it. {NOTHING_WRITTEN}"
            )
        case RefusalWording.CLEANUP:
            # No cleanup row refuses a horizon today (they all floor it); the text is the cleanup one if one ever does.
            return FactBornDueError(
                f"counted from when the owner said it ({local_date(reported_date, timezone).isoformat()}), a horizon of "
                f"{fields.get('ttl_days')} days {ended} {expires_on}: this pass reviews stored memory: do not drop the "
                f"fact. Give at least {_min_horizon_days(reported_date, now)} days, or rewrite it as what happened "
                f"(past tense, the outcome if known) with {{kind: none}}. {NOTHING_WRITTEN}"
            )
        case RefusalWording.REVIEW:
            return FactBornDueError(f"this horizon {ended} {expires_on}, too soon: {_REVIEW_WAY_OUT}. {NOTHING_WRITTEN}")
        case _:
            assert_never(wording)


def _date_refusal(
    expires: datetime, reported_date: datetime, now: datetime, timezone: str, wording: RefusalWording
) -> FactBornDueError:
    last_day = local_date(expires - timedelta(microseconds=1), timezone)
    today = local_date(now, timezone)
    # A date after today can land here only on a 23-hour DST day, in its last hour.
    plan = "a plan for today is not over yet" if last_day == today else f"a plan ending {last_day.isoformat()} is too close"
    past = f"the date {last_day.isoformat()} is past (today is {today.isoformat()} in {timezone})"
    match wording:
        case RefusalWording.REVIEW:
            # Chosen before any DISCARD text: the review and the owner hold stored memory, never a candidate.
            return FactBornDueError(f"{past if last_day < today else plan}: {_REVIEW_WAY_OUT}. {NOTHING_WRITTEN}")
        case RefusalWording.CLEANUP if last_day < today:
            # The subject is a stored fact, not a candidate: dropping it loses memory.
            return FactBornDueError(
                f"{past}: this pass reviews "
                "stored memory: do not drop the fact because its date passed. Rewrite it as what happened (past tense, the outcome if known) "
                f"with {{kind: none}}, or give a future date if the plan still holds. {NOTHING_WRITTEN}"
            )
        case RefusalWording.STAGE1 if last_day < today:
            return FactBornDueError(
                f"{past}: DISCARD the "
                "candidate if it no longer matters, or rewrite the fact as what happened (past tense, the outcome if "
                f"known) with {{kind: none}}. {NOTHING_WRITTEN}"
            )
        case RefusalWording.STAGE1 | RefusalWording.CLEANUP:
            return FactBornDueError(
                f"{plan}: give a short horizon of at least "
                f"{_min_horizon_days(reported_date, now)} days so it is reviewed after it happened. {NOTHING_WRITTEN}"
            )
        case _:
            assert_never(wording)


def _min_horizon_days(reported_date: datetime, now: datetime) -> int:
    """The smallest N whose horizon still passes the rule a day from now (the one-day margin: the model answers later)."""
    return max(1, (now + 2 * _DAY - local_moment(reported_date, "UTC")) // _DAY + 1)


def expiry_pinned(domain: Optional[FactDomain], type: FactType, tags: Sequence[str]) -> bool:
    """Directives, principles and mindset facts take `none` only: their lifecycle stays with Stage 2b."""
    return domain == FactDomain.AGENT_DIRECTIVE or type == FactType.PRINCIPLE or "mindset" in {t.lower() for t in tags}


def check_pin(expiry: Optional[Expiry], pinned: bool) -> Expiry:
    """The decision to write. A pinned fact gets `none` when none was sent and refuses anything else.

    An unpinned fact must carry a decision; the caller words the "missing" text for its own place before calling.
    """
    if pinned:
        if expiry is None or expiry.kind is ExpiryKind.NONE:
            return EXPIRY_NONE
        raise FactExpiryError(f"directives, principles and mindset facts take {{kind: none}}. {NOTHING_WRITTEN}")
    if expiry is None:
        raise FactExpiryError(f"expiry is required for this fact. {NOTHING_WRITTEN}")
    return expiry


def required_decision(expiry: Optional[Expiry], pinned: bool, missing: Callable[[], FactExpiryError]) -> Expiry:
    """The decision to write: required unless the fact is pinned (a pinned one gets `none`), then `check_pin`.

    `missing` builds the refusal for the caller's own place; it is raised when an unpinned fact carries no decision.
    """
    if expiry is None and not pinned:
        raise missing()
    return check_pin(expiry, pinned)


def lifecycle_event(
    at: datetime, from_state: Optional[FactState], to_state: FactState, actor: str, reason: str
) -> Dict[str, Any]:
    """One `lifecycle_events` entry. `reason` is code-built, never model text."""
    return {
        "at": at,
        "from": from_state.value if from_state is not None else None,
        "to": to_state.value,
        "actor": actor,
        "reason": reason,
    }


def with_event(events: Sequence[Mapping[str, Any]], event: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """`events` plus `event`, the newest LIFECYCLE_EVENTS_CAP kept."""
    return [dict(e) for e in [*events, event]][-LIFECYCLE_EVENTS_CAP:]


def revival_fields(
    fact: FactEntity, expiry_fields: Mapping[str, Any], now: datetime, actor: str, reason: str
) -> Dict[str, Any]:
    """The in-place write that brings `fact` (stale, or current and waiting for an answer) back to current: state current,
    `stale_since` cleared, `expiry_fields` (empty = the decision stays as stored), and one trail entry under `actor` with
    the code-built `reason`, stamped `now` (the moment a horizon in `expiry_fields` was counted from, so the window a
    later silence doubles is exact). The entry is written even when the fact was already current.

    The one builder for a review revive, an owner's answer and a question's fallback."""
    event = lifecycle_event(now, fact.state, FactState.CURRENT, actor, reason)
    return {
        **lifecycle_fields(FactState.CURRENT, now),
        **expiry_fields,
        "stale_since": None,
        "last_updated": now,
        "lifecycle_events": with_event(fact.lifecycle_events, event),
    }
