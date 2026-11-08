"""InboundGapTracker: gaps in inbound packets, above a floor, armed by the first packet."""
from src.domain.inbound_gap_tracker import InboundGapTracker


def test_nothing_before_the_first_packet():
    t = InboundGapTracker(floor_s=2.0)
    assert t.gap_started(100.0) is False
    assert t.packet(100.0) is None


def test_gap_below_floor_is_silent():
    t = InboundGapTracker(floor_s=2.0)
    t.packet(0.0)
    assert t.gap_started(1.9) is False
    assert t.packet(1.9) is None


def test_gap_start_once_then_end_with_duration():
    t = InboundGapTracker(floor_s=2.0)
    t.packet(0.0)
    assert t.gap_started(2.1) is True
    assert t.gap_started(3.0) is False
    assert t.packet(5.0) == 5.0


def test_gap_end_reported_even_if_start_poll_missed():
    t = InboundGapTracker(floor_s=2.0)
    t.packet(0.0)
    assert t.packet(4.0) == 4.0


def test_new_gap_after_recovery_is_announced_again():
    t = InboundGapTracker(floor_s=2.0)
    t.packet(0.0)
    assert t.gap_started(2.5) is True
    t.packet(3.0)
    assert t.gap_started(5.5) is True
