"""save_to_memory persists only on an interactive chat turn.

Only ConversationHandler attaches `consolidation_text` to stored history; notify(), the
Lelik gateway and /worker runs drop it. A save there must say so instead of `saved: True`.
"""

from unittest.mock import AsyncMock

from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.ports.embedding_service import EmbeddingService
from src.ports.repository import FactRepository

_USER_ID = "user-abc123"
_PASSAGE = "User weighs 80 kg. Mentioned in diet discussion."


def _agent() -> FactsMemoryAgent:
    return FactsMemoryAgent(
        config=AgentConfig(agent_id=f"memory_search_agent_{_USER_ID}", agent_type="memory_search"),
        repository=AsyncMock(spec=FactRepository),
        embedding_service=AsyncMock(spec=EmbeddingService),
        account_id="account-abc123",
    )


def _save(context: dict, payload: dict | None = None) -> AgentMessage:
    return AgentMessage(
        intent=AgentIntent.QUERY,
        payload=payload or {"intent": "save_to_memory", "text": _PASSAGE},
        sender="smart_response_agent",
        recipient=f"memory_search_agent_{_USER_ID}",
        task_id="task-1",
        context={"user_id": _USER_ID, **context},
    )


async def test_interactive_turn_saves():
    response = await _agent().execute(_save({"interactive_delivery": True}))

    assert response.status == AgentStatus.SUCCESS
    assert response.result == {"saved": True}
    assert response.history_context == {"consolidation_text": _PASSAGE}


async def test_background_run_is_not_saved_and_says_so():
    response = await _agent().execute(_save({}))

    assert response.status == AgentStatus.SUCCESS
    assert response.result["saved"] is False
    assert "chat" in response.result["reason"]
    assert not response.history_context


async def test_falsy_interactive_flag_is_not_saved():
    response = await _agent().execute(_save({"interactive_delivery": False}))

    assert response.result["saved"] is False
    assert not response.history_context


async def test_empty_text_still_fails_before_the_path_check():
    response = await _agent().execute(_save({}, {"intent": "save_to_memory", "query": ""}))

    assert response.status == AgentStatus.FAILED
    assert "empty text" in response.error
