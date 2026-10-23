"""SCD2 flags that go with a fact state."""

from datetime import datetime, time, timedelta
from typing import AbstractSet, Any, Dict, Final, List, Mapping, Optional, Sequence, Tuple

from .entities import ExpiryKind, FactDomain, FactEntity, FactState, FactType
from .exceptions import FactExpiryError, FactResolutionError
from .fact_expiry import EXPIRY_NONE, NOTHING_WRITTEN, Expiry, as_expiry_kind
from .local_time import local_date, local_moment, zone_or_utc

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
            raise FactResolutionError(f"fact {start_id} was superseded and its successor is unknown; search again")
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


def born_due(
    fields: Mapping[str, Any], reported_date: datetime, now: datetime, timezone: str, cleanup: bool
) -> Tuple[Dict[str, Any], Optional[str]]:
    """No expiry is born due: `fields` (from compute_expiry) as they may be written, and an optional note for the model.

    A decision that resolves at or before now + 1 day is refused with FactExpiryError. The one exception is a horizon on a
    cleanup pass (stage 2a/2b, backstop, cluster), which cannot move `reported_date`: it is floored to now + 2 days, so
    the fact reaches review soon.
    """
    kind, expires_at = as_expiry_kind(fields.get("expiry_kind")), fields.get("expires_at")
    if kind not in (ExpiryKind.DATE, ExpiryKind.HORIZON) or expires_at is None:
        return dict(fields), None
    now = local_moment(now, "UTC")
    expires = local_moment(expires_at, "UTC")
    if expires > now + _DAY:
        return dict(fields), None
    if kind is ExpiryKind.DATE:
        raise _date_refusal(expires, reported_date, now, timezone)
    if cleanup:
        review_on = now + _CLEANUP_FLOOR
        note = (
            f"counted from {local_date(reported_date, timezone).isoformat()} this horizon had already passed; "
            f"the fact goes to review on {local_date(review_on, timezone).isoformat()}"
        )
        return {**fields, "expires_at": review_on}, note
    raise FactExpiryError(
        f"counted from when the owner said it ({local_date(reported_date, timezone).isoformat()}), a horizon of "
        f"{fields.get('ttl_days')} days {'ended' if expires <= now else 'ends'} {local_date(expires, timezone).isoformat()}: "
        f"give at least {_min_horizon_days(reported_date, now)} days, rewrite the fact as history with {{kind: none}}, "
        f"or DISCARD it. {NOTHING_WRITTEN}"
    )


def _date_refusal(expires: datetime, reported_date: datetime, now: datetime, timezone: str) -> FactExpiryError:
    last_day = local_date(expires - timedelta(microseconds=1), timezone)
    today = local_date(now, timezone)
    if last_day < today:
        return FactExpiryError(
            f"the date {last_day.isoformat()} is past (today is {today.isoformat()} in {timezone}): DISCARD the "
            "candidate if it no longer matters, or rewrite the fact as what happened (past tense, the outcome if "
            f"known) with {{kind: none}}. {NOTHING_WRITTEN}"
        )
    # A date after today can land here only on a 23-hour DST day, in its last hour.
    plan = "a plan for today is not over yet" if last_day == today else f"a plan ending {last_day.isoformat()} is too close"
    return FactExpiryError(
        f"{plan}: give a short horizon of at least "
        f"{_min_horizon_days(reported_date, now)} days so it is reviewed after it happened. {NOTHING_WRITTEN}"
    )


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
