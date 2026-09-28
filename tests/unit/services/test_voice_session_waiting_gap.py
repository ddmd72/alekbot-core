"""Waiting notes during a delegation come after a random gap, not on a fixed beat (owner, 2026-09-28)."""
from unittest.mock import AsyncMock, MagicMock

from src.ports.alert_sink import AlertSinkPort
from src.ports.call_control_plane_port import CallControlPlanePort
from src.services.voice_session_service import VoiceSessionService


def _service(**kwargs):
    return VoiceSessionService(
        realtime_session_factory=MagicMock(),
        control_plane=AsyncMock(spec=CallControlPlanePort),
        alert_sink=AsyncMock(spec=AlertSinkPort),
        **kwargs,
    )


def test_gap_is_drawn_between_silence_timeout_and_max():
    service = _service(silence_timeout_s=8.0, waiting_gap_max_s=15.0)
    gaps = [service._next_waiting_gap() for _ in range(500)]
    assert all(8.0 <= gap <= 15.0 for gap in gaps)
    assert len({round(gap, 3) for gap in gaps}) > 1


def test_without_max_the_gap_stays_the_silence_timeout():
    service = _service(silence_timeout_s=8.0)
    assert service._next_waiting_gap() == 8.0


def test_max_not_above_floor_falls_back_to_silence_timeout():
    service = _service(silence_timeout_s=8.0, waiting_gap_max_s=5.0)
    assert service._next_waiting_gap() == 8.0
