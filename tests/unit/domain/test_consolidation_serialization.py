from src.domain.consolidation_serialization import serialize_messages_for_consolidation
from src.domain.companion_config import CompanionTextMode
from src.domain.llm import Message, MessagePart


def _model_msg(text=None, full_text=None):
    return Message(role="model", parts=[MessagePart(text=text, full_text=full_text)], created_at=1000.0)


def _user_msg(text=None, consolidation_text=None):
    return Message(role="user", parts=[MessagePart(text=text, consolidation_text=consolidation_text)], created_at=1000.0)


def test_summary_mode_uses_text_for_model_parts():
    result = serialize_messages_for_consolidation([_model_msg(text="short", full_text="verbose full response")], text_mode=CompanionTextMode.SUMMARY)
    assert result[0]["parts"][0]["text"] == "short"


def test_summary_mode_is_the_default():
    result = serialize_messages_for_consolidation([_model_msg(text="short", full_text="verbose")])
    assert result[0]["parts"][0]["text"] == "short"


def test_full_mode_prefers_full_text_for_model_parts():
    result = serialize_messages_for_consolidation([_model_msg(text="short", full_text="verbose full response")], text_mode=CompanionTextMode.FULL)
    assert result[0]["parts"][0]["text"] == "verbose full response"


def test_full_mode_falls_back_to_text_when_full_text_absent():
    result = serialize_messages_for_consolidation([_model_msg(text="short", full_text=None)], text_mode=CompanionTextMode.FULL)
    assert result[0]["parts"][0]["text"] == "short"


def test_user_parts_prefer_consolidation_text_regardless_of_mode():
    for mode in (CompanionTextMode.SUMMARY, CompanionTextMode.FULL):
        result = serialize_messages_for_consolidation(
            [_user_msg(text="raw", consolidation_text="explicit save")], text_mode=mode,
        )
        assert result[0]["parts"][0]["text"] == "explicit save"


def test_user_parts_fall_back_to_text_when_no_consolidation_text():
    result = serialize_messages_for_consolidation([_user_msg(text="raw", consolidation_text=None)])
    assert result[0]["parts"][0]["text"] == "raw"


def test_empty_parts_are_dropped_not_emitted_as_blank():
    result = serialize_messages_for_consolidation([_model_msg(text=None, full_text=None)])
    assert result[0]["parts"] == []


def test_role_and_created_at_are_preserved():
    result = serialize_messages_for_consolidation([_user_msg(text="hi")])
    assert result[0]["role"] == "user"
    assert result[0]["created_at"] == 1000.0
