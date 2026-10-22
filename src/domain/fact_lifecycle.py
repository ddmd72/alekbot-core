"""SCD2 flags that go with a fact state."""

from datetime import datetime
from typing import AbstractSet, Any, Optional

from .entities import FactEntity, FactState
from .exceptions import FactResolutionError

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
    {"state", "is_current", "valid_to", "superseded_by", "reported_date", "last_updated"}
)

MAX_FORWARD_HOPS = 50


def forward_step(
    fact: FactEntity,
    accepted: AbstractSet[FactState],
    origin_lineage_id: str,
    start_id: str,
    origin_account_id: str,
) -> Optional[str]:
    """One step of following `start_id` to the fact a caller may write: None = `fact` is it, a str = read that id next.

    Raises FactResolutionError when the chain cannot end in `accepted`. The state is read before `superseded_by`.
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
        return fact.superseded_by
    if fact.id == start_id:
        raise FactResolutionError(f"fact {start_id} is {fact.state.value}", current_id=fact.id)
    raise FactResolutionError(
        f"fact {start_id} was replaced by {fact.id}, which is {fact.state.value}", current_id=fact.id
    )


def closure_fields(successor_id: str, now: datetime) -> dict[str, Any]:
    """The in-place write that closes a fact in favour of `successor_id`."""
    return {**lifecycle_fields(FactState.SUPERSEDED, now), "superseded_by": successor_id, "last_updated": now}
