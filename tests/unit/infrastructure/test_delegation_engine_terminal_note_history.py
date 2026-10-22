"""A local tool co-emitted with the terminal `deliver_response` (Grok) keeps its history line unless it is `use_skill`
(final review C-M6 / L-M6): a drive note written in that batch keeps its `[Drive note: … ref=…]` line, so "undo that"
on the next turn has the ref; a skill body that was never followed still leaves nothing."""
from unittest.mock import AsyncMock, MagicMock

from src.infrastructure.drive_note_tools import DRIVE_NOTE_CONTEXT_KEY
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult

LINE = "[Drive note: Docs/a.pdf ref=drive:a v2]"


def _handler(history):
    async def handle(tc: ToolCall) -> ToolResult:
        return ToolResult(name=tc.name, result_str="ok", history_context=history)
    return AsyncMock(side_effect=handle)


async def test_a_note_written_beside_deliver_response_keeps_its_history_line_and_use_skill_does_not():
    note = _handler({DRIVE_NOTE_CONTEXT_KEY: LINE})
    skill = _handler({"skill_context": {"name": "a", "version": 1, "body": "BODY"}})
    call_llm = AsyncMock(return_value=LLMResponse(text="", tool_calls=[
        ToolCall(name="use_skill", args={"name": "a"}),
        ToolCall(name="annotate_drive_file", args={"file_ref": "drive:a", "note": "x"}),
        ToolCall(name="deliver_response", args={"full_response": "noted"}),
    ]))
    result = await DelegationEngine(MagicMock(spec=AgentCoordinator)).execute(
        call_llm=call_llm, base_request=LLMRequest(model_name="grok", messages=[
            Message(role="user", parts=[MessagePart(text="note it")])]),
        context={}, max_turns=3, terminal_tool="deliver_response",
        local_tools={"use_skill": skill, "annotate_drive_file": note},
    )
    note.assert_awaited_once()
    assert result.terminal_tool_args == {"full_response": "noted"}
    assert result.history_contexts == {DRIVE_NOTE_CONTEXT_KEY: [LINE]}
