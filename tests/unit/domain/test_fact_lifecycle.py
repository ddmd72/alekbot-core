"""lifecycle_fields: SCD2 flags per fact state."""

from datetime import datetime, timezone

import pytest

from src.domain.entities import FactState
from src.domain.fact_lifecycle import lifecycle_fields

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "state,is_current,closed",
    [
        (FactState.CURRENT, True, False),
        (FactState.STALE, True, False),
        (FactState.ARCHIVED, True, True),
        (FactState.INVALIDATED, True, True),
        (FactState.SUPERSEDED, False, True),
    ],
)
def test_state_mapping(state, is_current, closed):
    assert lifecycle_fields(state, NOW) == {
        "state": state,
        "is_current": is_current,
        "valid_to": NOW if closed else None,
    }


@pytest.mark.parametrize("state", [FactState.ARCHIVED, FactState.INVALIDATED, FactState.SUPERSEDED])
def test_closing_states_use_the_given_now(state):
    assert lifecycle_fields(state, NOW)["valid_to"] is NOW


def test_state_is_enum_not_value():
    assert lifecycle_fields(FactState.CURRENT, NOW)["state"] is FactState.CURRENT


def test_every_state_is_mapped():
    for state in FactState:
        lifecycle_fields(state, NOW)
