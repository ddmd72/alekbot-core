"""The consolidation agent passes the caller account to FactManagementPort.update_fact."""

from unittest.mock import AsyncMock, MagicMock

from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.llm import ToolCall
from src.ports.fact_management_port import FactManagementPort


def _agent(fm):
    agent = ConsolidationAgent.__new__(ConsolidationAgent)
    agent._fact_management = fm
    return agent


async def test_directive_cap_backstop_passes_account_id():
    fm = AsyncMock(spec=FactManagementPort)
    agent = _agent(fm)
    agent.DIRECTIVE_HARD_CAP = 1
    facts = [MagicMock(id=f"f{i}") for i in range(3)]
    agent._repo = MagicMock()
    agent._repo.get_active_facts_ordered = AsyncMock(return_value=facts)
    await agent._enforce_directive_cap("acc-1", agent._write_context("backstop"))
    assert fm.update_fact.await_count == 2
    for call in fm.update_fact.await_args_list:
        assert call.kwargs["account_id"] == "acc-1"


async def test_tool_dispatch_passes_account_id():
    fm = AsyncMock(spec=FactManagementPort)
    fm.update_fact.return_value = {"status": "updated"}
    agent = _agent(fm)
    call = ToolCall(name="update_fact", args={"fact_id": "f1", "updates": {"state": "archived"}})
    context = agent._write_context("stage1")
    await agent._execute_fact_management_tools([call], "usr-1", "acc-1", context)
    fm.update_fact.assert_awaited_once_with(
        fact_id="f1", updates={"state": "archived"}, account_id="acc-1", context=context
    )
