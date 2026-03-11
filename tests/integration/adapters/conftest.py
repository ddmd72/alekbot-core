"""
CapturingStub fixtures for adapter integration tests.

Each stub replaces the SDK client method on a real adapter instance,
captures what kwargs the adapter sends, and returns a valid domain
LLMResponse so generate_content() can complete without errors.

The captured data is then validated against ContractRule objects from
tests/contracts/adapter_contracts.py — the "rule repository".

Usage:
    adapter = ClaudeAdapter(api_key="test-key")
    stub = ClaudeCapturingStub().install(adapter)
    await adapter.generate_content(request=...)
    SOME_CONTRACT.validate("claude", stub.captured_kwargs)
"""
import json
from unittest.mock import MagicMock, AsyncMock


# ============================================================================
# Shared mock response builders
# ============================================================================

def _claude_text_response(text="OK"):
    block = MagicMock()
    block.type = "text"
    block.text = text

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def _claude_tool_response(name, args, tc_id="call_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = args
    block.id = tc_id

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def _gemini_text_response(text="OK"):
    part = MagicMock()
    part.text = text
    part.function_call = None

    content = MagicMock()
    content.parts = [part]

    candidate = MagicMock()
    candidate.content = content
    candidate.grounding_metadata = None

    usage = MagicMock()
    usage.prompt_token_count = 10
    usage.candidates_token_count = 5
    usage.total_token_count = 15

    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    return response


def _openai_text_response(text="OK"):
    message = MagicMock()
    message.content = text
    message.tool_calls = None

    choice = MagicMock()
    choice.message = message

    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15

    completion = MagicMock()
    completion.choices = [choice]
    completion.usage = usage
    return completion


def _openai_tool_response(name, args, tc_id="call_1"):
    tc = MagicMock()
    tc.id = tc_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)

    message = MagicMock()
    message.content = None
    message.tool_calls = [tc]

    choice = MagicMock()
    choice.message = message

    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15

    completion = MagicMock()
    completion.choices = [choice]
    completion.usage = usage
    return completion


# ============================================================================
# CapturingStub implementations — one per adapter SDK boundary
# ============================================================================

class ClaudeCapturingStub:
    """
    Captures kwargs sent to Claude's client.messages.stream().
    Install on a real ClaudeAdapter instance before calling generate_content().
    """

    def __init__(self, sdk_response=None):
        self.captured_kwargs: dict = {}
        self._sdk_response = sdk_response or _claude_text_response()

    def install(self, adapter) -> "ClaudeCapturingStub":
        stub = self
        stream = AsyncMock()
        stream.get_final_message = AsyncMock(return_value=stub._sdk_response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=stream)
        cm.__aexit__ = AsyncMock(return_value=None)

        def capturing_stream(**kwargs):
            stub.captured_kwargs.update(kwargs)
            return cm

        adapter.client.messages.stream = capturing_stream
        return self

    @classmethod
    def with_tool_response(cls, name, args, tc_id="call_1") -> "ClaudeCapturingStub":
        return cls(sdk_response=_claude_tool_response(name, args, tc_id))


class GeminiCapturingStub:
    """
    Captures kwargs sent to Gemini's client.aio.models.generate_content().
    The captured dict contains: {"model": ..., "contents": ..., "config": ...}.
    """

    def __init__(self, sdk_response=None):
        self.captured_kwargs: dict = {}
        self._sdk_response = sdk_response or _gemini_text_response()

    def install(self, adapter) -> "GeminiCapturingStub":
        stub = self

        async def mock_generate(model=None, contents=None, config=None):
            stub.captured_kwargs["model"] = model
            stub.captured_kwargs["contents"] = contents
            stub.captured_kwargs["config"] = config
            return stub._sdk_response

        adapter.client = MagicMock()
        adapter.client.aio.models.generate_content = mock_generate
        return self


class OpenAILikeCapturingStub:
    """
    Captures kwargs sent to client.chat.completions.create().
    Works for both OpenAIAdapter and GrokAdapter (same SDK pattern).
    """

    def __init__(self, sdk_response=None):
        self.captured_kwargs: dict = {}
        self._sdk_response = sdk_response or _openai_text_response()

    def install(self, adapter) -> "OpenAILikeCapturingStub":
        stub = self

        async def mock_create(**kwargs):
            stub.captured_kwargs.update(kwargs)
            return stub._sdk_response

        adapter.client = MagicMock()
        adapter.client.chat.completions.create = mock_create
        return self

    @classmethod
    def with_tool_response(cls, name, args, tc_id="call_1") -> "OpenAILikeCapturingStub":
        return cls(sdk_response=_openai_tool_response(name, args, tc_id))
