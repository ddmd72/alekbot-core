"""
Unit tests for FactsMemoryAgent: the intent it accepts and the fields its rendering leaves out.

The `queries` request, the search and the full rendering are in `test_memory_search_agent_v2.py`.

Tests cover:
- can_handle: wrong intent
- the shared result formatter: absent optional fields omitted
"""

from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.entities import FactEntity, FactType
from src.domain.fact_search_result import PhraseSearchResult
from src.domain.memory_search_format import format_search_result
from src.ports.fact_search_port import FactSearchPort

_ACCOUNT_ID = "account-abc123"
_USER_ID = "user-abc123"


class _UntouchedSearch(FactSearchPort):
    """Neither test searches: any call fails the test."""

    async def search(self, account_id, query, *, exclude=frozenset()):
        raise AssertionError("no search expected")

    async def search_phrases(self, account_id, queries):
        raise AssertionError("no search expected")


class _UntouchedSlots:
    async def query_for_request(self, request, *, path, account_id, user_id):
        raise AssertionError("no slots call expected")


def _make_agent() -> FactsMemoryAgent:
    return FactsMemoryAgent(
        config=AgentConfig(
            agent_id=f"memory_search_agent_{_USER_ID}",
            agent_type="memory_search",
        ),
        fact_search=_UntouchedSearch(),
        slots=_UntouchedSlots(),
        account_id=_ACCOUNT_ID,
        user_id=_USER_ID,
        user_location="Springfield, Portugal",
    )


class TestCanHandle:

    async def test_rejects_wrong_intent(self):
        agent = _make_agent()
        msg = AgentMessage(
            intent=AgentIntent.INFORM,
            payload={"query": "test"},
            sender="smart", recipient="memory_search_agent", task_id="t",
            context={"user_id": _USER_ID},
        )
        assert await agent.can_handle(msg) is False


class TestRenderedFact:

    def test_null_fields_omitted(self):
        # A FactEntity always carries its reported date (not optional) and an empty metadata dict when it has none;
        # the absent fields are context, metadata and the time points behind the `when:` line. The agent renders
        # through the shared formatter (memory_search_format), so the fact is rendered there.
        fact = FactEntity(
            account_id=_ACCOUNT_ID, created_by_user_id=_USER_ID, lineage_id="lin",
            text="Alex owns a 2015 Skoda Fabia", type=FactType.STATE,
            context=None, metadata={}, time_points=None,
        )
        result = format_search_result(["Alex car"], PhraseSearchResult((fact,), ((0,),)))
        assert "context:" not in result
        assert "metadata:" not in result
        assert "when:" not in result
