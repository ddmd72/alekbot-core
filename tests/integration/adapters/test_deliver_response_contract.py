"""
Integration tests for the SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL and SCHEMA_WITHOUT_TOOLS_USES_NATIVE_FORMAT
contracts (decisions/claude_deliver_response_string_tool.md).

Real adapter code, mocked SDK boundary: Claude through ClaudeCapturingStub, Grok (the twin mechanism) through
OpenAIResponsesCapturingStub.
"""
import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import (
    SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL,
    SCHEMA_WITHOUT_TOOLS_USES_NATIVE_FORMAT,
)
from tests.integration.adapters.conftest import ClaudeCapturingStub, OpenAIResponsesCapturingStub

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]
_TOOLS = [{"name": "search_memory", "description": "Search memories",
           "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}]
_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "rich_content": {"type": "object", "nullable": True,
                         "properties": {"kind": {"type": "string"}}},
    },
    "required": ["text"],
}


@pytest.mark.asyncio
async def test_claude_schema_with_tools_contract():
    adapter = ClaudeAdapter(api_key="test-key")
    stub = ClaudeCapturingStub().install(adapter)
    await adapter.generate_content(LLMRequest(
        model_name="claude-sonnet-5-5", system_instruction="test", messages=_MESSAGES,
        tools=_TOOLS, response_schema=_SCHEMA, thinking="medium"))
    SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL.validate(
        "claude", {**stub.captured_kwargs, "expected_schema": _SCHEMA})


@pytest.mark.asyncio
async def test_grok_schema_with_tools_contract():
    adapter = GrokAdapter(api_key="test-key")
    stub = OpenAIResponsesCapturingStub().install(adapter)
    await adapter.generate_content(LLMRequest(
        model_name="grok-4.6", messages=_MESSAGES, tools=_TOOLS, response_schema=_SCHEMA))
    SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL.validate(
        "grok", {**stub.captured_kwargs, "expected_parameters": adapter._to_json_schema(_SCHEMA)})


@pytest.mark.asyncio
async def test_claude_schema_without_tools_contract():
    adapter = ClaudeAdapter(api_key="test-key")
    stub = ClaudeCapturingStub().install(adapter)
    await adapter.generate_content(LLMRequest(
        model_name="claude-sonnet-5-5", system_instruction="test", messages=_MESSAGES,
        response_schema=_SCHEMA))
    SCHEMA_WITHOUT_TOOLS_USES_NATIVE_FORMAT.validate("claude", stub.captured_kwargs)
