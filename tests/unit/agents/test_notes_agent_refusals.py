"""NotesAgent refuses what it cannot store as sent (P14-D2/D3, P16-D5): the refusals beyond the e2e pins.

The pins in `test_notes_agent_tool_fields_e2e.py` cover an unknown `complexity` on create and an unparsable `due` on
update; these cover the same refusals on the other tool, and a `due` the clocks skip. Same rig: the real agent, coordinator
and note adapter on `EvalDb`, a scripted model; every assertion reads the stored documents and the delegation's answer.
"""
from tests.support.clock_instants import BERLIN, SKIPPED_HOUR
from tests.support.redelivery_world import require
from tests.support.scripted_llm import text, tools
from tests.unit.agents.test_notes_agent_tool_fields_e2e import (
    CREATE_BAD_COMPLEXITY, FAR_DUE, Rig, UPDATE_BAD_DUE, _call,
)
from src.domain.agent import AgentStatus

SKIPPED_DUE = SKIPPED_HOUR[BERLIN].naive.isoformat(timespec="minutes")   # the rig's user lives in Berlin


async def test_update_refuses_an_unknown_complexity_and_changes_nothing():
    rig = await Rig([tools(("update_self_reminder", UPDATE_BAD_DUE)), text("Updated.")]).start()
    note_id = await rig.seed(recurrence=None)
    rig.llm.script[0] = tools(("update_self_reminder", {"note_id": note_id, "text": "Call dad tonight", "complexity": "deep"}))
    before = dict(rig.docs[note_id])

    response = await rig.delegate()

    assert response.status == AgentStatus.FAILED
    assert "complexity" in (response.error or "") and "small_talk" in (response.error or "")
    assert rig.docs[note_id] == before
    assert rig.receipts == []
    assert rig.finished()


async def test_create_refuses_a_skipped_local_due_and_stores_nothing():
    rig = await Rig([tools(("create_self_reminder", {**CREATE_BAD_COMPLEXITY, "complexity": "small_talk",
                                                     "due": SKIPPED_DUE})),
                     text("Could not set it.")]).start()

    response = await rig.delegate()

    assert response.status == AgentStatus.FAILED
    assert "'due'" in (response.error or "") and SKIPPED_DUE in (response.error or "")
    assert rig.docs == {}
    assert rig.receipts == []
    assert rig.finished()


async def test_update_refuses_a_skipped_local_due_and_keeps_the_fire_time():
    rig = await Rig([tools(("update_self_reminder", UPDATE_BAD_DUE)), text("Could not move it.")]).start()
    note_id = await rig.seed(recurrence=None)
    rig.llm.script[0] = tools(("update_self_reminder", _call({"due": SKIPPED_DUE}, note_id)))

    response = await rig.delegate()

    require(rig.finished(), "every scripted answer was used and none was missing")
    assert response.status == AgentStatus.FAILED and "'due'" in (response.error or "")
    assert rig.docs[note_id]["due"] == FAR_DUE


async def test_create_without_a_due_still_names_the_missing_field():
    """Control: an absent due keeps its own error, distinct from an unusable one."""
    call = {k: v for k, v in CREATE_BAD_COMPLEXITY.items() if k not in ("due", "complexity")}
    rig = await Rig([tools(("create_self_reminder", call)), text("Could not set it.")]).start()

    response = await rig.delegate()

    assert response.status == AgentStatus.FAILED
    assert "requires 'due'" in (response.error or "")
    assert rig.docs == {}
    assert rig.finished()
