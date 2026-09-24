import inspect
from src.ports.call_control_plane_port import CallControlPlanePort


def test_call_control_plane_port_is_abstract():
    assert inspect.isabstract(CallControlPlanePort)


def test_call_control_plane_port_declares_slice1_methods():
    assert {"fetch_session_config", "submit_transcript"}.issubset(set(dir(CallControlPlanePort)))
