"""Cross-cut review fixes X1, X6, X9, X12 on the DriveAgent."""
import asyncio
from unittest.mock import AsyncMock

from src.agents import drive_agent as module
from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentStatus
from src.domain.drive_line import DriveLine
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_preflight import DrivePreflight
from src.domain.drive_search_line import SearchLine
from src.domain.drive_search import DISABLED_TEXT
from src.infrastructure.drive_operations import STILL_RUNNING
from tests.support.scripted_llm import calls, script
from tests.unit.agents.test_drive_agent import REPORT, _agent, _msg, _ok, _stalled, ops, prompts  # noqa: F401


async def test_x9_a_hanging_alert_sink_still_returns_the_answer(ops, prompts, monkeypatch):
    monkeypatch.setattr(module, "ALERT_TIMEOUT_S", 0.05)

    async def hang(text):
        await asyncio.sleep(30)
    sink = AsyncMock()
    sink.post.side_effect = hang
    agent = _agent(ops, prompts, alert_sink=sink)
    agent._call_llm, _ = script(*_stalled())
    resp = await asyncio.wait_for(agent.execute(_msg()), timeout=5)
    assert resp.status == AgentStatus.SUCCESS and resp.metadata["drive_status"] == "unfinished"


async def test_x12_smart_era_fields_reach_the_commission_as_stray_fields(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, seen = script(calls(REPORT))
    await agent.execute(_msg("find it", drive_query="lease", drive_folder="Contracts", drive_kind="pdf",
                             drive_date_from="2026-01-01", drive_date_to="2026-02-01", drive_deleted=True,
                             new_name="x.pdf", source_ref="drive:a", original=True, name="n"))
    text = seen[0].messages[0].parts[0].text
    assert text.startswith("find it\n\nAlso given with the commission:\n")
    for line in ("drive_query: lease", "drive_folder: Contracts", "drive_kind: pdf", "drive_date_from: 2026-01-01",
                 "drive_date_to: 2026-02-01", "drive_deleted: True", "new_name: x.pdf", "source_ref: drive:a",
                 "original: True", "name: n"):
        assert line in text


async def test_x12_empty_and_false_stray_fields_are_left_out(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, seen = script(calls(REPORT))
    await agent.execute(_msg("find it", drive_deleted=False, drive_query=""))
    assert seen[0].messages[0].parts[0].text == "find it"


async def test_x6_a_listing_only_answer_with_search_off_carries_the_disabled_text(ops, prompts):
    ops.preflight.return_value = DrivePreflight(DriveLine.CONNECTED, SearchLine.OFF)
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls((tt.LIST_FOLDER, {"folder": "Inbox"})), calls(REPORT))
    resp = await agent.execute(_msg("what is in Inbox"))
    assert DISABLED_TEXT in resp.result.splitlines()


async def test_x1_a_batch_of_two_moves_with_the_first_still_running_is_never_did_not_finish(ops, prompts):
    gate = asyncio.Event()

    async def first_then_second(context, user_id, ref, **kw):
        if ref == "drive:a":
            return DriveOutcome(op=DriveOp.MOVE, cls=OutcomeClass.STILL_RUNNING, text=STILL_RUNNING, success=True)
        gate.set()
        return _ok(DriveOp.MOVE, "Moved.")
    ops.move.side_effect = first_then_second
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls((tt.MOVE_FILE, {"file_ref": "drive:a", "folder": "X"}),
                                      (tt.MOVE_FILE, {"file_ref": "drive:b", "folder": "X"})), calls(REPORT))
    resp = await agent.execute(_msg("move both"))
    assert resp.metadata["drive_status"] == "still_running"
    assert t.DID_NOT_FINISH not in resp.result
    assert not gate.is_set()   # the second move never reached the service
