"""GeminiAdapter usage mapping, mocked at `client.aio.models.generate_content`: thought tokens are billed as output,
so they are part of `completion_tokens`; the input leg is uncached only."""
from unittest.mock import MagicMock

import pytest

from src.adapters.gemini_adapter import GeminiAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import INTERACTIVE_THOUGHTS_IN_COMPLETION_TOKENS


async def _usage(thoughts):
    part = MagicMock(text="ok", thought=False, function_call=None)
    candidate = MagicMock(grounding_metadata=None, finish_reason=None)
    candidate.content.parts = [part]
    response = MagicMock(candidates=[candidate])
    response.usage_metadata = MagicMock(prompt_token_count=1000, cached_content_token_count=200,
                                        candidates_token_count=100, thoughts_token_count=thoughts,
                                        total_token_count=1100 + (thoughts or 0))

    async def generate(model=None, contents=None, config=None):
        return response

    adapter = GeminiAdapter(api_key="test-key")
    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = generate
    request = LLMRequest(model_name="gemini-3.8-flash", system_instruction="read",
                         messages=[Message(role="user", parts=[MessagePart(text="hi")])])
    return (await adapter.generate_content(request=request)).usage_metadata


@pytest.mark.parametrize("thoughts", [1500, 0, None])
async def test_completion_tokens_include_thought_tokens(thoughts):
    usage = await _usage(thoughts)

    INTERACTIVE_THOUGHTS_IN_COMPLETION_TOKENS.validate("gemini", {"usage": usage, "candidates": 100, "thoughts": thoughts})
    assert usage.completion_tokens == 100 + (thoughts or 0)
    assert usage.prompt_tokens == 800
    assert usage.cache_read_tokens == 200
