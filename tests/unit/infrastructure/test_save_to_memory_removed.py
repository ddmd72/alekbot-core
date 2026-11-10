"""save_to_memory is gone (MEMORY_FACT_CONFIRMATION_RFC §6): no intent, no descriptor, no writer."""
import re
from pathlib import Path

from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.memory_search_texts import NO_QUERIES_NOTE
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, MEMORY_SEARCH, Intent
from src.ports.fact_search_port import FactSearchPort

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


class _UntouchedSearch(FactSearchPort):
    async def search(self, account_id, query, *, exclude=frozenset()):
        raise AssertionError("a stale save payload must not search")

    async def search_phrases(self, account_id, queries):
        raise AssertionError("a stale save payload must not search")


class _UntouchedSlots:
    async def query_for_request(self, request, *, path, account_id, user_id):
        raise AssertionError("a stale save payload must not reach the slots")


async def test_memory_agent_answers_a_stale_save_payload_with_the_tool_note_and_writes_nothing():
    # The agent takes every QUERY (FACT_PROFILE_RFC §5.4: a call without phrases comes back as a tool note), so a stale
    # save payload reaches execute: it gets the no-phrases note, nothing is searched, and there is no save handler.
    agent = FactsMemoryAgent(
        config=AgentConfig(agent_id="facts_memory_agent_u", agent_type="facts_memory"),
        fact_search=_UntouchedSearch(),
        slots=_UntouchedSlots(),
        account_id="a",
        user_id="u",
        user_location="Springfield, Portugal",
    )
    stale = AgentMessage.create(
        sender="t", recipient=agent.agent_id, intent=AgentIntent.QUERY,
        payload={"intent": "save_to_memory", "text": "User's lease ends in December 2027"},
    )
    response = await agent.execute(stale)
    assert response.result == NO_QUERIES_NOTE
    assert not hasattr(agent, "_handle_save")


def test_only_pass_through_sites_assign_consolidation_text():
    pattern = re.compile(r"\bconsolidation_text\s*=")
    found = set()
    for path in _SRC.rglob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            found.add(path.relative_to(_SRC).as_posix())
    # domain/llm.py declares the field with a type annotation (`consolidation_text: Optional[str] = None`),
    # which the pattern does not match; any other assignment is a new writer.
    assert found <= _ALLOWED_CONSOLIDATION_TEXT_ASSIGNMENTS, found - _ALLOWED_CONSOLIDATION_TEXT_ASSIGNMENTS
