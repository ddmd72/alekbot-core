"""`include_location`: the user's location is injected into the prompt unless the caller opts out (drive specialist)."""
from unittest.mock import AsyncMock, MagicMock

from src.domain.user import PromptPreferences, UserBotConfig
from src.ports.repository import FactRepository
from src.services.prompt_builder import UserPromptBuilder


def _builder():
    repo = MagicMock(spec=FactRepository)
    repo.get_biographical_context_cached = AsyncMock(return_value=[])
    assembly = MagicMock()
    assembly.assemble = AsyncMock(return_value="PROMPT")
    config = UserBotConfig(prompt_preferences=PromptPreferences(), location="Springfield")
    return UserPromptBuilder(repo=repo, user_id="u1", config=config, assembly_service=assembly), assembly


async def test_smart_keeps_the_user_location_by_default():
    builder, assembly = _builder()
    await builder.build_for_agent(agent_type="smart", user_id="u1")
    assert assembly.assemble.call_args.kwargs["user_location"] == "Springfield"


async def test_include_location_false_drops_it_from_the_assembly():
    builder, assembly = _builder()
    await builder.build_for_agent(agent_type="drive", user_id="u1", include_location=False)
    assert assembly.assemble.call_args.kwargs["user_location"] is None
