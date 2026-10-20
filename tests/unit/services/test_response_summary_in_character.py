"""response_summary is written in Alek's character, up to 60 words / 600 chars."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.core.smart_response_agent import SmartResponseAgent
from src.ports.llm_port import LLMResponse
from src.services.history_summary_service import HistorySummaryService


def test_smart_schema_summary_allows_600_chars_and_asks_for_character():
    prop = SmartResponseAgent._RESPONSE_SCHEMA["properties"]["response_summary"]
    assert prop["maxLength"] == 600
    desc = prop["description"].lower()
    assert "60 words" in desc and "character" in desc and "emoji" in desc


@pytest.mark.asyncio
async def test_fallback_summariser_prompt_states_the_new_limit():
    provider = MagicMock()
    response = MagicMock(spec=LLMResponse)
    response.text = json.dumps({"summary": "ok 🙂"})
    provider.generate_content = AsyncMock(return_value=response)
    await HistorySummaryService(llm_port=provider, model_name="m").summarize_model_response("text")
    prompt = provider.generate_content.await_args.kwargs["request"].system_instruction
    assert "60 words" in prompt and "600 chars" in prompt
    assert "300" not in prompt
