"""save_to_memory is gone (MEMORY_FACT_CONFIRMATION_RFC §6): no intent, no descriptor, no writer."""
import pytest
import re
from pathlib import Path
from unittest.mock import AsyncMock

from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, MEMORY_SEARCH, Intent
from src.ports.embedding_service import EmbeddingService
from src.ports.repository import FactRepository

_SRC = Path(__file__).resolve().parents[3] / "src"

# Pass-through / legacy sites only: they copy an existing value, none creates one from new text.
_ALLOWED_CONSOLIDATION_TEXT_ASSIGNMENTS = {
    "agents/base_agent.py",
    "adapters/openai_adapter.py",
    "adapters/grok_adapter.py",
    "adapters/firestore_session_store.py",
}


def test_intent_has_no_save_to_memory():
    assert not hasattr(Intent, "SAVE_TO_MEMORY")
    assert "save_to_memory" not in {v for k, v in vars(Intent).items() if not k.startswith("_")}


def test_no_descriptor_exposes_save_to_memory():
    for d in ALL_DESCRIPTORS:
        assert "save_to_memory" not in d.capabilities, d.agent_id
        assert "save_to_memory" not in d.capability_descriptions, d.agent_id
        assert "save_to_memory" not in (d.context_schemas or {}), d.agent_id
    assert set(MEMORY_SEARCH.capabilities) == {Intent.SEARCH_MEMORY}


async def test_memory_agent_rejects_a_stale_save_payload():
    agent = FactsMemoryAgent(
        config=AgentConfig(agent_id="facts_memory_agent_u", agent_type="facts_memory"),
        repository=AsyncMock(spec=FactRepository),
        embedding_service=AsyncMock(spec=EmbeddingService),
        account_id="a",
    )
    stale = AgentMessage.create(
        sender="t", recipient=agent.agent_id, intent=AgentIntent.QUERY,
        payload={"intent": "save_to_memory", "text": "User's lease ends in December 2027"},
    )
    assert await agent.can_handle(stale) is False
    assert not hasattr(agent, "_handle_save")


@pytest.mark.datafile
def test_only_pass_through_sites_assign_consolidation_text():
    pattern = re.compile(r"\bconsolidation_text\s*=")
    found = set()
    for path in _SRC.rglob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            found.add(path.relative_to(_SRC).as_posix())
    # domain/llm.py declares the field with a type annotation (`consolidation_text: Optional[str] = None`),
    # which the pattern does not match; any other assignment is a new writer.
    assert found <= _ALLOWED_CONSOLIDATION_TEXT_ASSIGNMENTS, found - _ALLOWED_CONSOLIDATION_TEXT_ASSIGNMENTS
