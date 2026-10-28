"""DriveAgent, the drive specialist (DRIVE_SPECIALIST_RFC §5; §11.1 "Agent", "Time", "State block").

The LLM is scripted at `_call_llm`; DriveOperations is a spec mock (its own behaviour is covered by its tests), except
where one shared instance across two executions is the point."""
import asyncio
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.drive_agent import DriveAgent, origin_label, render_state, soft_deadline
from src.domain import drive_tool_texts as tt
from src.domain import drive_note_tool_texts as nt
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_file_content_item import FileContentItem
from src.domain.drive_note import DRIVE_NOTE_RECEIPT_DELIVERY, NoteReceipt
from src.domain.drive_line import DriveLine
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_preflight import DrivePreflight
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_search_line import SearchLine
from src.domain.llm import PROMPT_CACHE_BOUNDARY, LLMResponse, MessagePart, ToolCall
from src.domain.retry_policy import NO_RETRY_POLICY
from src.domain.run_deadline import CURRENT_RUN_DEADLINE
from src.domain.turn_clock import CURRENT_TURN_CLOCK, TurnClock
from src.domain.user_drive import DriveItem, MoveOutcome
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.delegation_engine import DelegationEngine
from src.infrastructure.drive_operations import DriveOperations
from src.ports.llm_port import AgentExecutionContext
from src.services.drive_note_service import DriveNoteService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.scripted_llm import calls, script

FILE = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
PROMPT = f"DriveAgent extends Agent {{ … }}\n\n{PROMPT_CACHE_BOUNDARY}\nknowledge_base {{}}"
CONNECTED = DrivePreflight(DriveLine.CONNECTED, SearchLine.ON)


def _ok(op, text, **kw):
    return DriveOutcome(op=op, cls=OutcomeClass.OK, text=text, success=True, **kw)


@pytest.fixture
def ops():
    o = AsyncMock(spec=DriveOperations)
    o.preflight.return_value = CONNECTED
    o.folder_path.side_effect = lambda user_id, folder: folder
    o.search.return_value = _ok(DriveOp.SEARCH, "REMINDER\n<card lease>", items=(FILE,), facts=ReadFacts(cards=1))
    o.list_folder.return_value = _ok(DriveOp.LIST, "[Drive: Inbox ref=drive:i]")
    o.move.return_value = _ok(DriveOp.MOVE, "Moved: Inbox/lease.pdf → [Drive: Archive/lease.pdf ref=drive:f1].",
                              items=(FILE,))
    return o


@pytest.fixture
def prompts():
    p = AsyncMock()
    p.build_for_agent.return_value = PROMPT
    return p


def _agent(ops, prompts, **kw):
    ctx = MagicMock(spec=AgentExecutionContext)
    ctx.provider, ctx.model_name = AsyncMock(), "claude-sonnet-5-5"
    agent = DriveAgent(AgentConfig(agent_id="drive_agent_u1", agent_type="drive", timeout_ms=DRIVE.timeout_ms),
                       execution_context=ctx, operations=ops, prompt_builder=prompts, user_timezone="Europe/Madrid",
                       clock=lambda: NOW, **kw)
    agent.coordinator = MagicMock()
    agent.coordinator.handle_delegation = AsyncMock()
    return agent


def _msg(query="find my lease", context=None, **payload):
    return AgentMessage.create(sender="coordinator", recipient="drive_agent_u1", intent=AgentIntent.QUERY,
                               payload={"query": query, "intent": "work_with_drive_files", **payload},
                               context={"user_id": "u1", "account_id": "a1", "interactive_delivery": True,
                                        **(context or {})})


REPORT = (tt.REPORT_TOOL, {"note": "Searched for the lease."})


async def test_a_find_returns_success_with_the_code_built_answer(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, seen = script(calls((tt.SEARCH_DRIVE, {"query": "lease"})), calls(REPORT))
    resp = await agent.execute(_msg())
    assert resp.status == AgentStatus.SUCCESS and resp.metadata["drive_status"] == "ok"
    assert resp.result.startswith("Drive result: ok\nWhat to do: Answer from the results below.")
    assert "[Result r1, step 1: search]\nREMINDER\n<card lease>" in resp.result
    assert "Specialist note: Searched for the lease." in resp.result
    assert resp.history_context == {"drive_context": [{"path": "Inbox/lease.pdf", "ref": "drive:f1"}]}
    assert "Correct your input" not in resp.result and len(seen) == 2


async def test_every_exit_is_a_success_even_a_failed_change(ops, prompts):
    ops.move.return_value = DriveOutcome(op=DriveOp.MOVE, cls=OutcomeClass.NOT_FOUND, success=False,
                                         text="That item is no longer on the drive (deleted or moved out). List again.")
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls((tt.MOVE_FILE, {"file_ref": "drive:f1", "folder": "A"})), calls(REPORT))
    resp = await agent.execute(_msg("move it"))
    assert resp.status == AgentStatus.SUCCESS and resp.metadata["drive_status"] == "not_found"
    assert "What to do: That item is no longer on the drive" in resp.result


async def test_smart_receives_the_answer_without_the_correct_your_input_suffix(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls((tt.MOVE_FILE, {"file_ref": "drive:f1"})), calls(REPORT))
    coordinator = MagicMock()

    async def handle(**kw):
        return await agent.execute(_msg("move it"))
    coordinator.handle_delegation = AsyncMock(side_effect=handle)
    result = await DelegationEngine(coordinator).dispatch(
        ToolCall(name="delegate_to_specialist", args={"intent": "work_with_drive_files", "query": "move it"}),
        {"user_id": "u1"}, {}, {}, "smart")
    assert "Correct your input" not in result.result_str and not result.failed
    assert result.result_str.startswith("Drive result: refused")


async def test_a_missing_prompt_fails_before_any_llm_call(ops, prompts):
    prompts.build_for_agent.side_effect = RuntimeError("profile drive not found")
    agent = _agent(ops, prompts)
    agent._call_llm = AsyncMock()
    resp = await agent.execute(_msg())
    assert resp.status == AgentStatus.FAILED and "PromptBuilder failed" in resp.error
    agent._call_llm.assert_not_called()
    no_builder = _agent(ops, None)
    no_builder._call_llm = AsyncMock()
    assert (await no_builder.execute(_msg())).status == AgentStatus.FAILED
    no_builder._call_llm.assert_not_called()


async def test_the_prompt_has_no_directives_and_no_biography(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls(REPORT))
    await agent.execute(_msg())
    kwargs = prompts.build_for_agent.await_args.kwargs
    assert kwargs["agent_type"] == "drive" and kwargs["include_directives"] is False
    assert kwargs["include_biographical"] is False


async def test_a_drive_that_is_not_configured_answers_at_once(ops, prompts):
    ops.preflight.return_value = DrivePreflight(DriveLine.NOT_CONFIGURED, SearchLine.OFF)
    agent = _agent(ops, prompts)
    agent._call_llm = AsyncMock()
    resp = await agent.execute(_msg())
    assert resp.status == AgentStatus.SUCCESS and resp.metadata["drive_status"] == "failed"
    assert "What to do: The drive is not configured." in resp.result
    agent._call_llm.assert_not_called()


async def test_no_retry_policy_and_no_commission_in_the_start_log(ops, prompts, caplog):
    agent = _agent(ops, prompts)
    assert agent.retry_policy is NO_RETRY_POLICY
    agent._call_llm, _ = script(calls(REPORT))
    with caplog.at_level(logging.INFO):
        await agent.execute(_msg("find the lease about Rua Fictícia 12"))
    assert not any("Rua Fictícia" in r.getMessage() for r in caplog.records)
    assert any("[DriveAgent] user=u1 status=" in r.getMessage() for r in caplog.records)


async def test_a_stray_delegation_is_an_unknown_tool(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, seen = script(
        calls(("delegate_to_specialist", {"intent": "search_web", "query": "q"})), calls(REPORT))
    await agent.execute(_msg())
    agent.coordinator.handle_delegation.assert_not_called()
    second = seen[1].messages[-1].parts[0].tool_response["response"]["result"]
    assert "unknown tool 'delegate_to_specialist'" in second


async def test_the_request_carries_the_tools_the_thinking_and_the_state_block(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, seen = script(calls(REPORT))
    parts = [MessagePart(text="put this in Contracts"),
             MessagePart(text='[File: "scan.pdf" (12KB)]', file_data={"ref": "scan.pdf", "mime_type": "application/pdf"})]
    await agent.execute(_msg("keep the attached scan in Contracts", context={"current_message_parts": parts},
                             file_contents=[{"name": "n.md", "text": "note"}], folder="Contracts"))
    request = seen[0]
    assert [t["name"] for t in request.tools] == [d["name"] for d in tt.drive_tool_declarations()]
    assert request.thinking == "medium" and request.max_tokens == DRIVE.max_tokens
    static, dynamic = request.system_instruction.split(PROMPT_CACHE_BOUNDARY)
    assert "drive_state {" not in static
    assert 'user_message (chat): "put this in Contracts"' in dynamic
    assert 'attached_now: [File: "scan.pdf" (12KB)]' in dynamic
    assert 'file_contents: t1 name "n.md" 4 characters, first line "note"' in dynamic
    assert "now: 2026-10-22 14:00 Europe/Madrid" in dynamic
    assert request.messages[0].parts[0].text == (
        "keep the attached scan in Contracts\n\nAlso given with the commission:\nfolder: Contracts")


async def test_a_users_thinking_wins(ops, prompts):
    agent = _agent(ops, prompts, thinking="low")
    agent._call_llm, seen = script(calls(REPORT))
    await agent.execute(_msg())
    assert seen[0].thinking == "low"


@pytest.mark.parametrize("context, label", [
    ({"interactive_delivery": True}, "chat"),
    ({"run_origin": "voice:ask_alek"}, "call (Lelik's words)"),
    ({"run_origin": "background:fire_due_reminders"}, "background: fire_due_reminders"),
    ({}, "fallback"),
])
def test_user_message_is_labelled_by_origin(context, label):
    assert origin_label(context) == label
    state = render_state({**context, "current_message_parts": [MessagePart(text="hi")]}, (),
                         CONNECTED, NOW, "UTC")
    assert f'user_message ({label}): "hi"' in state


def test_the_state_block_shows_no_item_text_beyond_the_first_line():
    item = FileContentItem("t1", "line one\nsecret line two", name="a.md")
    state = render_state({}, (item,), DrivePreflight(DriveLine.RECONNECT_NEEDED, SearchLine.BUILDING), NOW, "Nowhere/X")
    assert "secret" not in state and "drive: reconnect needed" in state and "search: still building" in state
    assert "now: 2026-10-22 12:00 UTC" in state


async def test_a_plain_text_reply_is_the_note(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls((tt.SEARCH_DRIVE, {"query": "lease"})),
                                 LLMResponse(text="I searched the drive for the lease.", tool_calls=[]))
    resp = await agent.execute(_msg())
    assert "Specialist note: I searched the drive for the lease." in resp.result


async def test_several_vision_files_reach_the_orchestrator_as_a_list(ops, prompts):
    ops.open.side_effect = [
        _ok(DriveOp.OPEN, "a attached", items=(FILE,), file_data={"path": "/tmp/a.jpg", "mime_type": "image/jpeg"}),
        _ok(DriveOp.OPEN, "b attached", items=(FILE,), file_data={"path": "/tmp/b.pdf", "mime_type": "application/pdf"})]
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls((tt.OPEN_DRIVE_FILE, {"file_ref": "drive:a"}),
                                        (tt.OPEN_DRIVE_FILE, {"file_ref": "drive:b"}), REPORT))
    resp = await agent.execute(_msg("show both photos"))
    assert [f["path"] for f in resp.metadata["file_data"]] == ["/tmp/a.jpg", "/tmp/b.pdf"]


async def test_the_receipt_of_a_note_stored_before_the_run_was_cut_reaches_the_answer(ops, prompts):
    # Design Δ7: delivery items come from the pass-through collector, not the engine's fold; a cut run has no engine
    # result, so this is the path that must still carry the receipt of a note stored before the cut.
    async def slow(*a, **k):
        await asyncio.sleep(5)
    ops.search.side_effect = slow
    receipt = NoteReceipt(action="create", note_id="n1", ref="drive:f1", path="Inbox/lease.pdf", version=1,
                          text="signed in March", status="not_indexed", state="active")
    notes = AsyncMock(spec=DriveNoteService)
    notes.create.return_value = receipt
    agent = _agent(ops, prompts, notes=notes)
    agent._call_llm, _ = script(calls((nt.CREATE_NOTE, {"item_ref": "drive:f1", "text": "signed in March"})),
                                 calls((tt.SEARCH_DRIVE, {"query": "lease"})))
    token = CURRENT_RUN_DEADLINE.set(time.monotonic() + DRIVE.run_deadline_margin_s + 0.3)
    try:
        resp = await agent.execute(_msg())
    finally:
        CURRENT_RUN_DEADLINE.reset(token)
    assert resp.metadata["drive_status"] == "unfinished"
    notes.create.assert_awaited_once()
    assert [(i.type, i.data) for i in resp.delivery_items] == [(DRIVE_NOTE_RECEIPT_DELIVERY, receipt.delivery_data())]


async def test_turns_exhausted_without_a_report_still_answers(ops, prompts):
    agent = _agent(ops, prompts)
    searches = [calls((tt.SEARCH_DRIVE, {"query": f"w{i}"})) for i in range(DRIVE.max_turns)]
    agent._call_llm, seen = script(*searches)
    resp = await agent.execute(_msg())
    assert len(seen) == DRIVE.max_turns and resp.status == AgentStatus.SUCCESS
    assert resp.metadata["drive_status"] == "unfinished"   # §7.2: no report, whatever finished (I1)


async def test_still_running_ends_the_commission_and_keeps_the_mutation(ops, prompts):
    ops.delete.return_value = DriveOutcome(op=DriveOp.DELETE, cls=OutcomeClass.STILL_RUNNING, success=True,
                                           text="The drive operation is still running and may complete.")
    agent = _agent(ops, prompts)
    agent._call_llm, seen = script(calls((tt.DELETE_DRIVE_ITEM, {"file_ref": "drive:f1"})), calls(REPORT))
    resp = await agent.execute(_msg("delete it"))
    assert len(seen) == 1 and resp.metadata["drive_status"] == "still_running"
    assert "What to do: The drive operation is still running and may complete." in resp.result


async def test_a_read_running_at_the_soft_deadline_ends_the_run_with_the_earlier_change_intact(ops, prompts):
    async def slow(*a, **k):
        await asyncio.sleep(5)
    ops.search.side_effect = slow
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls((tt.MOVE_FILE, {"file_ref": "drive:f1", "folder": "Archive"})),
                                 calls((tt.SEARCH_DRIVE, {"query": "lease"})))
    token = CURRENT_RUN_DEADLINE.set(time.monotonic() + DRIVE.run_deadline_margin_s + 0.3)
    try:
        started = time.monotonic()
        resp = await agent.execute(_msg())
    finally:
        CURRENT_RUN_DEADLINE.reset(token)
    assert time.monotonic() - started < 2
    assert resp.metadata["drive_status"] == "unfinished"
    assert "1. done: Moved: Inbox/lease.pdf" in resp.result
    assert "[Result r2, step 2: search]\ndid not finish in time" in resp.result


async def test_an_llm_call_is_clamped_to_the_time_left(ops, prompts):
    agent = _agent(ops, prompts)
    agent._call_llm, seen = script(calls(REPORT))
    token = CURRENT_RUN_DEADLINE.set(time.monotonic() + DRIVE.run_deadline_margin_s + 30)
    try:
        await agent.execute(_msg())
    finally:
        CURRENT_RUN_DEADLINE.reset(token)
    assert 28 <= seen[0].timeout <= 30


def test_the_soft_deadline_takes_the_earliest_bound():
    assert soft_deadline(100.0, 100.0) == 100.0 + DRIVE.soft_deadline_s
    token = CURRENT_RUN_DEADLINE.set(200.0)
    clock_token = CURRENT_TURN_CLOCK.set(TurnClock.start(budget_s=200, wrap_up_reserve_s=120, now=100.0))
    try:
        # turn clock: now + (200 - 120) - 15 = 165; run deadline: 200 - 20 = 180; own: 250
        assert soft_deadline(100.0, 100.0) == 165.0
    finally:
        CURRENT_TURN_CLOCK.reset(clock_token)
        CURRENT_RUN_DEADLINE.reset(token)


async def test_one_user_lock_across_two_concurrent_executions(prompts):
    """Two commissions of one user, one shared DriveOperations: their moves never overlap (§5.5)."""
    drive = AsyncMock(spec=UserDriveService)
    order = []

    async def move(user_id, item_id, folder, new_name):
        order.append(("start", folder))
        await asyncio.sleep(0.05)
        order.append(("end", folder))
        return MoveOutcome(before=FILE, after=FILE, created=[])
    drive.move.side_effect = move
    drive.connection.return_value = MagicMock(connected=True, needs_reconnect=False)
    shared = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive)
    first, second = _agent(shared, prompts), _agent(shared, prompts)
    first._call_llm, _ = script(calls((tt.MOVE_FILE, {"file_ref": "drive:f1", "folder": "A"}), REPORT))
    second._call_llm, _ = script(calls((tt.MOVE_FILE, {"file_ref": "drive:f1", "folder": "B"}), REPORT))
    await asyncio.gather(first.execute(_msg("to A")), second.execute(_msg("to B")))
    assert order in ([("start", "A"), ("end", "A"), ("start", "B"), ("end", "B")],
                     [("start", "B"), ("end", "B"), ("start", "A"), ("end", "A")])


async def test_a_batch_runs_in_emission_order(ops, prompts):
    order = []

    async def move(context, user_id, ref, *, folder, new_name, wait_s):
        order.append(folder)
        await asyncio.sleep(0.01 if folder == "A" else 0)
        return _ok(DriveOp.MOVE, f"Moved to {folder}")
    ops.move.side_effect = move
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls((tt.MOVE_FILE, {"file_ref": "drive:1", "folder": "A"}),
                                        (tt.MOVE_FILE, {"file_ref": "drive:2", "folder": "B"}),
                                        (tt.MOVE_FILE, {"file_ref": "drive:3", "folder": "C"})), calls(REPORT))
    resp = await agent.execute(_msg("move all"))
    assert order == ["A", "B", "C"]
    assert resp.result.index("Moved to A") < resp.result.index("Moved to B") < resp.result.index("Moved to C")


# -- I3: a turn-cap exit without a report is loud, and says nothing private ------------------------------------------------

LONG_USER = "u-12345678-private-tail"


def _stalled(alert_sink=None):
    return [calls((tt.SEARCH_DRIVE, {"query": f"secret lease {i}"}), (tt.LIST_FOLDER, {"folder": "Private/Taxes"}))
            for i in range(DRIVE.max_turns)]


async def test_a_turn_cap_exit_logs_error_with_the_tool_sequence_and_nothing_private(ops, prompts, caplog):
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(*_stalled())
    with caplog.at_level(logging.INFO):
        resp = await agent.execute(_msg(context={"user_id": LONG_USER}))
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    line = errors[0]
    assert "hit the turn limit" in line and "user=u-123456 " in line
    assert f"1: {tt.SEARCH_DRIVE},{tt.LIST_FOLDER}" in line and f"{DRIVE.max_turns}: " in line
    for private in ("secret", "lease", "Private", "Taxes", "private-tail", LONG_USER):
        assert private not in line
    assert resp.metadata["drive_status"] == "unfinished"


async def test_a_turn_cap_exit_posts_one_alert_through_the_sink(ops, prompts):
    sink = AsyncMock()
    agent = _agent(ops, prompts, alert_sink=sink)
    agent._call_llm, _ = script(*_stalled())
    await agent.execute(_msg(context={"user_id": LONG_USER}))
    sink.post.assert_awaited_once()
    text = sink.post.await_args.args[0]
    assert text.startswith("🚨 Drive specialist hit the turn limit without a report")
    assert f"turns {DRIVE.max_turns}/{DRIVE.max_turns}" in text and "user u-123456" in text
    assert tt.SEARCH_DRIVE in text
    for private in ("secret", "Private", "Taxes", "private-tail"):
        assert private not in text


async def test_no_sink_means_the_error_log_only(ops, prompts, caplog):
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(*_stalled())
    with caplog.at_level(logging.INFO):
        resp = await agent.execute(_msg())
    assert resp.status == AgentStatus.SUCCESS
    assert sum(1 for r in caplog.records if r.levelno == logging.ERROR) == 1


async def test_a_failing_sink_never_breaks_the_answer(ops, prompts):
    sink = AsyncMock()
    sink.post.side_effect = RuntimeError("slack down")
    agent = _agent(ops, prompts, alert_sink=sink)
    agent._call_llm, _ = script(*_stalled())
    assert (await agent.execute(_msg())).metadata["drive_status"] == "unfinished"


async def test_a_report_or_a_deadline_is_not_a_turn_cap_alert(ops, prompts, caplog):
    sink = AsyncMock()
    agent = _agent(ops, prompts, alert_sink=sink)
    agent._call_llm, _ = script(calls((tt.SEARCH_DRIVE, {"query": "lease"})), calls(REPORT))
    with caplog.at_level(logging.INFO):
        await agent.execute(_msg())
    sink.post.assert_not_called()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_operations_is_the_shared_instance_read_only(ops, prompts):
    agent = _agent(ops, prompts)
    assert agent.operations is ops
    with pytest.raises(AttributeError):
        agent.operations = object()


async def test_the_preflight_and_the_prompt_are_read_together(ops, prompts):
    """M13: neither waits for the other."""
    both_started = asyncio.Event()
    started = []

    async def preflight(user_id):
        started.append("preflight")
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        return CONNECTED

    async def build(**kwargs):
        started.append("prompt")
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        return PROMPT
    ops.preflight.side_effect = preflight
    prompts.build_for_agent.side_effect = build
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls(REPORT))
    assert (await agent.execute(_msg())).status == AgentStatus.SUCCESS
