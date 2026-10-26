"""`session_id_for` (T12c R3): the one key rule of a per-channel chat session."""
from src.domain.messaging import session_id_for


def test_the_session_is_the_user_and_the_channel():
    assert session_id_for("u1", "C123") == "u1:C123"
    assert session_id_for("u1", "D9") != session_id_for("u1", "C123")
    assert session_id_for("u2", "C123") != session_id_for("u1", "C123")


def test_a_telegram_chat_id_and_a_slack_dm_follow_the_same_rule():
    assert session_id_for("u1", "-1001234") == "u1:-1001234"
