"""drive_cap_alert.cap_flag_changes: the once-per-incident transition of the fan-out's cap alert, per cap."""
import pytest

from src.domain.drive_cap_alert import CARD_CAP, CHUNK_CAP, CapCounts, cap_flag_changes


@pytest.mark.parametrize("counts, card_alerted, chunk_alerted, expected", [
    (CapCounts(), False, False, {}),                                        # nothing held, nothing alerted
    (CapCounts(card=10), False, False, {CARD_CAP: True}),                   # the card cap starts holding
    (CapCounts(chunk=10), False, False, {CHUNK_CAP: True}),                 # the chunk cap starts holding
    (CapCounts(card=10, chunk=20), False, False, {CARD_CAP: True, CHUNK_CAP: True}),
    (CapCounts(card=10, chunk=20), True, True, {}),                         # the same incident: no change
    (CapCounts(), True, True, {CARD_CAP: False, CHUNK_CAP: False}),         # both cleared
    (CapCounts(card=10), True, True, {CHUNK_CAP: False}),                   # one clears, the other still holds
    (CapCounts(chunk=1), True, False, {CARD_CAP: False, CHUNK_CAP: True}),  # one clears while the other starts
])
def test_flag_changes(counts, card_alerted, chunk_alerted, expected):
    assert cap_flag_changes(counts, card_alerted=card_alerted, chunk_alerted=chunk_alerted) == expected


def test_the_card_cap_comes_first():
    assert list(cap_flag_changes(CapCounts(card=1, chunk=1), card_alerted=False, chunk_alerted=False)) == [
        CARD_CAP, CHUNK_CAP]
