from unittest.mock import AsyncMock

from src.domain.long_turn import LongTurnRecord
from src.domain.llm import ToolCall
from src.domain.turn_clock import RUNNING_JOBS_HEADER
from src.infrastructure.long_turn_tools import (
    CANCEL_LONG_TURN_TOOL,
    build_cancel_long_turn_tool_declaration,
    make_cancel_long_turn_handler,
    render_running_jobs,
)


def _rec(turn_id="slack:Ev123456", title="compare offers", started=0.0, step="tool: search_web"):
    return LongTurnRecord(turn_id=turn_id, user_id="u1", session_id="s", title=title,
                          started_at=started, heartbeat_at=started, step=step)


def test_render_lists_title_elapsed_step_and_short_id():
    out = render_running_jobs([_rec()], now=300.0)
    assert out.startswith(RUNNING_JOBS_HEADER)
    assert "compare offers" in out and "5 min" in out and "search_web" in out and "id=123456" in out


def test_declaration_requires_id():
    decl = build_cancel_long_turn_tool_declaration()
    assert decl["name"] == CANCEL_LONG_TURN_TOOL
    assert decl["parameters"]["required"] == ["id"]


async def test_handler_cancels_by_short_id():
    registry = AsyncMock()
    registry.request_cancel.return_value = True
    handle = make_cancel_long_turn_handler(registry, "u1", [_rec()])
    result = await handle(ToolCall(name=CANCEL_LONG_TURN_TOOL, args={"id": "123456"}))
    registry.request_cancel.assert_awaited_with("u1", "slack:Ev123456")
    assert "cancel requested" in result.result_str


async def test_unknown_id_is_a_failed_result():
    handle = make_cancel_long_turn_handler(AsyncMock(), "u1", [_rec()])
    result = await handle(ToolCall(name=CANCEL_LONG_TURN_TOOL, args={"id": "nope"}))
    assert result.failed
