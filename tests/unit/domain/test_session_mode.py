from src.domain.session_mode import SessionMode


def test_default_is_full_orchestrator_flow():
    mode = SessionMode()
    assert mode.history_source == "session_store"
    assert mode.route_intent is None
    assert mode.write_session is True
    assert mode.write_consolidation is True
    assert mode.write_session_id is None
    assert mode.is_bound is False


def test_write_session_id_defaults_to_none():
    mode = SessionMode(route_intent="tutor_chat", write_session=True)
    assert mode.write_session_id is None


def test_write_session_id_can_be_set():
    mode = SessionMode(route_intent="tutor_chat", write_session=True, write_session_id="slack:C1")
    assert mode.write_session_id == "slack:C1"


def test_is_bound_true_when_route_intent_set():
    mode = SessionMode(route_intent="tutor_chat")
    assert mode.is_bound is True
