"""The history helpers of Smart that guard a turn's transcript: the one-line summary, the consistency check and the
orphan sanitizer, on the shapes the other tests do not reach. Real `SmartResponseAgent` over a scripted model."""
import pytest

from src.domain.llm import Message, MessagePart, ToolCall
from tests.support.scripted_llm import ScriptedLLM
from tests.support.tool_e2e import smart_rig


@pytest.fixture
def agent():
    return smart_rig(ScriptedLLM([]), user_id="usr-1")


def test_a_part_with_no_content_is_listed_without_a_kind(agent):
    history = [Message(role="user", parts=[MessagePart()])]
    assert agent._summarize_history(history) == "user()"


def test_a_single_message_history_is_valid(agent):
    agent._validate_history([Message(role="user", parts=[MessagePart(text="compare the irrigation kits")])])


def test_alternating_roles_are_valid(agent):
    agent._validate_history([
        Message(role="user", parts=[MessagePart(text="compare the irrigation kits")]),
        Message(role="model", parts=[MessagePart(text="on it")]),
    ])


def test_a_history_that_is_only_a_dangling_tool_call_sanitizes_to_nothing(agent):
    call = ToolCall(name="search_memory", args={})
    assert agent._sanitize_tool_history([Message(role="model", parts=[MessagePart(tool_call=call)])]) == []
