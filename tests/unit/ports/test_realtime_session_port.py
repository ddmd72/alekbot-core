from src.ports.realtime_session_port import RealtimeSessionEvent


def test_realtime_session_event_is_plain_value_object():
    event = RealtimeSessionEvent(type="tool_call", payload={"call_id": "c1"})
    assert event.type == "tool_call"
    assert event.payload == {"call_id": "c1"}
