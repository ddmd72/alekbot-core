from src.ports.call_control_plane_port import CallControlPlanePort


def test_port_declares_delegate():
    assert "delegate" in CallControlPlanePort.__abstractmethods__
