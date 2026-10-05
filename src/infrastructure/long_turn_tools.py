"""Smart's view of its own long turns: a status note and a cancel tool (RFC §5.4).

What the note means and when to cancel is PROTOCOL_LONG_TURNS; this file only renders data.
"""
from typing import Any, Dict, List

from ..domain.long_turn import LongTurnRecord
from ..domain.llm import ToolCall
from ..domain.turn_clock import RUNNING_JOBS_HEADER
from ..ports.long_turn_registry import LongTurnRegistry
from .delegation_engine import LocalToolHandler, ToolResult

CANCEL_LONG_TURN_TOOL = "cancel_long_turn"
_SHORT_ID = 6


def _short(turn_id: str) -> str:
    return turn_id[-_SHORT_ID:]


def render_running_jobs(running: List[LongTurnRecord], now: float) -> str:
    lines = [RUNNING_JOBS_HEADER]
    for r in running:
        minutes = max(0, int((now - r.started_at) // 60))
        lines.append(f'- "{r.title}" — {minutes} min, now: {r.step} (id={_short(r.turn_id)})')
    return "\n".join(lines)


def build_cancel_long_turn_tool_declaration() -> Dict[str, Any]:
    return {
        "name": CANCEL_LONG_TURN_TOOL,
        "description": "Stop one of your own long turns that is still running in the background.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The id shown in the running-jobs note."}},
            "required": ["id"],
        },
    }


def make_cancel_long_turn_handler(registry: LongTurnRegistry, user_id: str,
                                  running: List[LongTurnRecord]) -> LocalToolHandler:
    by_short = {_short(r.turn_id): r for r in running}

    async def handle(tool_call: ToolCall) -> ToolResult:
        short = str((tool_call.args or {}).get("id", "")).strip()
        record = by_short.get(short)
        if record is None:
            return ToolResult(name=CANCEL_LONG_TURN_TOOL, result_str=f"no running job with id={short}", failed=True)
        ok = await registry.request_cancel(user_id, record.turn_id)
        msg = "cancel requested; it stops within ~30 s" if ok else "it already finished"
        return ToolResult(name=CANCEL_LONG_TURN_TOOL, result_str=msg, failed=not ok)

    return handle
