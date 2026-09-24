import inspect
from src.ports.realtime_session_port import RealtimeSessionPort, RealtimeSessionEvent


def test_realtime_session_port_is_abstract():
    assert inspect.isabstract(RealtimeSessionPort)


def test_realtime_session_port_declares_expected_methods():
    expected = {"open", "send_audio", "receive_events", "submit_tool_result", "submit_message", "request_response", "close"}
    assert expected.issubset(set(dir(RealtimeSessionPort)))


def test_realtime_session_event_is_plain_value_object():
    event = RealtimeSessionEvent(type="tool_call", payload={"call_id": "c1"})
    assert event.type == "tool_call"
    assert event.payload == {"call_id": "c1"}
