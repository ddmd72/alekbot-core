"""The query a specialist receives starts with a stamp that names the weekday and the year."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.delegation_timestamp import strip_delegation_timestamp
from src.infrastructure import agent_coordinator as coordinator_module
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentDescriptor, ExecutionMode
from tests.support import clock_instants as ci


async def test_delegated_query_is_stamped_with_weekday_and_year(monkeypatch: pytest.MonkeyPatch):
    ci.pin_datetime(monkeypatch, coordinator_module, datetime(2026, 11, 13, 9, 5, tzinfo=timezone.utc))
    registry = MagicMock()
    registry.get_agent_for_intent = MagicMock(return_value=AgentDescriptor(
        agent_id="memory_agent", capabilities={"search_memory": ExecutionMode.SYNC}, dispatch_deadline_s=300))
    coordinator = AgentCoordinator(registry=registry)
    seen = AsyncMock(return_value=AgentResponse.success(task_id="t", agent_id="memory_agent", result={}))
    monkeypatch.setattr(coordinator, "_execute_sync", seen)

    await coordinator.handle_delegation("search_memory", "find the invoice", {"user_id": "u"})

    query = seen.call_args.args[2]
    assert query.startswith("[Fri 13 Nov 2026, 09:05 UTC] ")
    assert strip_delegation_timestamp(query) == "find the invoice"
