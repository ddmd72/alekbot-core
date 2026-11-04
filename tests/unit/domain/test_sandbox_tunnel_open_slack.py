"""The byte budget's per-OPEN slack (S5 design §2.3 "Bounds", Δ13 amended): an open-ended range that ffmpeg closes
leaves bytes in flight past its window (h2 flow-control windows + pipe buffers), so `TunnelLedger` allows
`budget_bytes + opens so far x OPEN_SLACK_BYTES`. The formula of `tunnel_budget` is unchanged; the slack grows only with
OPENs, a byte past it is still a violation, and OPENs past MAX_OPENS are still refused, so the worst case stays
bounded by `tunnel_budget(size) + MAX_OPENS x OPEN_SLACK_BYTES`."""
import pytest

from src.domain.sandbox_tunnel import (
    DATA_MAX, MAX_OPENS, OPEN_SLACK_BYTES, TunnelLedger, TunnelViolation, tunnel_budget,
)

MiB = 1024 * 1024


def _fill(ledger: TunnelLedger, stream: int, start: int, total: int) -> int:
    """Sends `total` bytes on `stream` from `start` in DATA_MAX chunks; returns the next offset."""
    offset = start
    while total > 0:
        chunk = min(DATA_MAX, total)
        ledger.data(stream, offset, chunk)
        offset += chunk
        total -= chunk
    return offset


def test_the_slack_is_eight_mib_and_the_formula_is_unchanged():
    assert OPEN_SLACK_BYTES == 8 * MiB
    assert tunnel_budget(100 * MiB) == 264 * MiB


def test_the_allowance_grows_by_the_slack_with_every_open():
    ledger = TunnelLedger(source_bytes=100 * MiB, budget_bytes=4 * MiB)
    assert ledger.allowed_bytes == 4 * MiB
    for stream in range(1, 4):
        ledger.open(stream, 0)
        ledger.close(stream)
        assert ledger.allowed_bytes == 4 * MiB + stream * OPEN_SLACK_BYTES


def test_bytes_past_the_budget_are_allowed_within_the_slack_of_the_opens_so_far():
    ledger = TunnelLedger(source_bytes=100 * MiB, budget_bytes=4 * MiB)
    ledger.open(1, 0)
    _fill(ledger, 1, 0, 4 * MiB + OPEN_SLACK_BYTES)          # exactly the allowance: accepted
    assert ledger.bytes_in == ledger.allowed_bytes
    with pytest.raises(TunnelViolation, match="budget"):
        ledger.data(1, 4 * MiB + OPEN_SLACK_BYTES, 1)


def test_dropped_bytes_of_a_closed_stream_count_against_the_same_allowance():
    ledger = TunnelLedger(source_bytes=100 * MiB, budget_bytes=1 * MiB)
    ledger.open(1, 0)
    ledger.close(1)
    assert _fill(ledger, 1, 0, 1 * MiB + OPEN_SLACK_BYTES) == 1 * MiB + OPEN_SLACK_BYTES   # in flight, dropped
    with pytest.raises(TunnelViolation, match="budget"):
        ledger.data(1, 0, 1)


def test_opens_past_the_cap_are_still_refused_so_the_slack_is_bounded():
    ledger = TunnelLedger(source_bytes=1000, budget_bytes=tunnel_budget(1000))
    for stream in range(1, MAX_OPENS + 1):
        ledger.open(stream, 0)
        ledger.close(stream)
    assert ledger.allowed_bytes == tunnel_budget(1000) + MAX_OPENS * OPEN_SLACK_BYTES
    with pytest.raises(TunnelViolation, match="OPEN"):
        ledger.open(MAX_OPENS + 1, 0)
    assert ledger.allowed_bytes == tunnel_budget(1000) + MAX_OPENS * OPEN_SLACK_BYTES
