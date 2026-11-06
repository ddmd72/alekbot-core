"""One failing call per tool surface, through the real handler: the result reads `Tool note:`, never `SYSTEM:`."""
from unittest.mock import AsyncMock

from src.domain.exceptions import MemoryFactsRejected
from src.domain.llm import ToolCall
from src.domain.memory_fact import PROPOSE_MEMORY_FACTS_TOOL
from src.domain.skill import Skill
from src.domain.workspace import WorkspaceReferences
from src.infrastructure.memory_fact_tools import make_propose_memory_facts_handler
from src.infrastructure.skill_tools import make_use_skill_handler
from src.infrastructure.workspace_tools import make_workspace_handler
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

SKILL = Skill(name="flight-status", description="Use when x.", body="1. Open the page.", version=2)
ROME_TRIP = "trips/rome-10-14-aug-2025.md"


async def test_skill_not_found_is_a_tool_note():
    r = await make_use_skill_handler([SKILL], visible=set())(ToolCall(name="use_skill", args={"name": "no-such"}))
    assert r.failed
    assert r.result_str.startswith("Tool note: No skill named 'no-such'")
    assert "SYSTEM" not in r.result_str


async def test_workspace_missing_path_is_a_tool_note():
    handle = make_workspace_handler(
        WorkspaceService(InMemoryWorkspaceStore()), "u1", "chat", None, WorkspaceReferences(),
    )
    r = await handle(ToolCall(name="workspace", args={"command": "view", "path": ROME_TRIP}))
    assert r.failed
    assert r.result_str.startswith("Tool note:")
    assert "SYSTEM" not in r.result_str


async def test_memory_proposal_rejected_is_a_tool_note():
    propose = AsyncMock(side_effect=MemoryFactsRejected("fact 1 text flagged by the security check"))
    fact = {"text": "Tom Baker owns the R&D plan.", "source": "Tom's plan.docx"}
    r = await make_propose_memory_facts_handler(propose)(
        ToolCall(name=PROPOSE_MEMORY_FACTS_TOOL, args={"facts": [fact]}),
    )
    assert r.failed
    assert r.result_str.startswith("Tool note: proposal rejected")
    assert "SYSTEM" not in r.result_str
