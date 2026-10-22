"""G3's case message (re-review of part B, F8): the six context fields the owner run sends to Smart."""
from scripts.validation.drive_eval.smart_dry_run import case_message


def test_the_case_message_is_an_interactive_turn_in_its_own_session_per_case_and_repeat():
    msg = case_message({"id": "c7", "text": "find my passport"}, "smart_response", "u1", "a1", 3)
    assert msg.sender == "drive_g3_dry_run"
    assert msg.recipient == "smart_response"
    assert msg.payload == {"text": "find my passport"}
    assert msg.context["user_id"] == "u1"
    assert msg.context["account_id"] == "a1"
    assert msg.context["session_id"] == "u1:drive_g3_dry_run:c7:3"
    parts = msg.context["current_message_parts"]
    assert [p.text for p in parts] == ["find my passport"]
    assert msg.context["interactive_delivery"] is True
