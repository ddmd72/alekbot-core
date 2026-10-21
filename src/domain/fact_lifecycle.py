"""SCD2 flags that go with a fact state."""

from datetime import datetime
from typing import Any

from .entities import FactState

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
