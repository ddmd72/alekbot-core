"""Items after the call's cap are counted, not read."""
from src.domain.memory_inbox import MAX_ITEMS_PER_CALL
from src.domain.memory_inbox_items import normalize_items


class _Tripwire(dict):
    def get(self, *args, **kwargs):
        raise AssertionError("an item past the cap was read")


def test_items_past_the_cap_are_counted_without_being_read():
    raw = [{"information": f"Clara booked the Coimbra weekend, note {n}"} for n in range(MAX_ITEMS_PER_CALL)]
    raw += [_Tripwire(information="late") for _ in range(5)]
    result = normalize_items(raw)
    assert len(result.drafts) == MAX_ITEMS_PER_CALL and result.over_call == 5 and result.dropped_empty == 0
