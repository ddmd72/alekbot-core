"""build_for_agent forwards its arguments to the assembly service (mutation survivors)."""
from unittest.mock import AsyncMock, MagicMock

from src.domain.user import PromptPreferences, UserBotConfig
from src.ports.repository import FactRepository
from src.services.prompt_builder import UserPromptBuilder


def _builder():
    repo = MagicMock(spec=FactRepository)
    repo.get_biographical_context_cached = AsyncMock(return_value=[])
    assembly = MagicMock()
    assembly.assemble = AsyncMock(return_value="PROMPT")
    config = UserBotConfig(prompt_preferences=PromptPreferences())
    return UserPromptBuilder(repo=repo, user_id="u1", config=config, assembly_service=assembly), assembly


async def _assemble_kwargs(**extra):
    builder, assembly = _builder()
    await builder.build_for_agent(agent_type="smart", user_id="u1", account_id="a1", **extra)
    return assembly.assemble.call_args.kwargs


async def test_conversation_history_is_forwarded():
    history = [{"role": "user", "content": "marker-history"}]
    kwargs = await _assemble_kwargs(conversation_history=history)
    assert kwargs["conversation_history"] == history


async def test_agent_notes_are_forwarded():
    notes = [{"text": "marker-note"}]
    kwargs = await _assemble_kwargs(agent_notes=notes)
    assert kwargs["agent_notes"] == notes


async def test_include_datetime_is_forwarded():
    assert (await _assemble_kwargs(include_datetime=True))["include_datetime"] is True
    assert (await _assemble_kwargs())["include_datetime"] is False
