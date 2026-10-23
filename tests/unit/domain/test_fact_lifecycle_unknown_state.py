"""Facts S3 · T14 done-check: a state with no lifecycle mapping is refused, never written as empty flags."""
from datetime import datetime, timezone

import pytest

from src.domain.entities import FactState
from src.domain.fact_lifecycle import lifecycle_fields

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def test_a_state_outside_the_lifecycle_map_raises_value_error_naming_it():
    with pytest.raises(ValueError, match="No lifecycle mapping for fact state") as caught:
        lifecycle_fields("archived-ish", NOW)  # type: ignore[arg-type]

    assert "archived-ish" in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize("state", list(FactState))
def test_every_fact_state_has_all_three_flags(state):
    assert set(lifecycle_fields(state, NOW)) == {"state", "is_current", "valid_to"}
