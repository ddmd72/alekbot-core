"""Notes, every declared field to the note store (TEST_COVERAGE_SYSTEMIC_RFC §5 P14, card D).

The real `NotesAgent` (its own `_call_llm`, tool loop and dispatch) behind a scripted `LLMPort`, reached the way Smart
reaches it: `AgentCoordinator.handle_delegation` over a registry holding the Notes descriptor, so the delegate field
`workspace_file` arrives through the `params` spread. The store is the real `FirestoreAgentNoteAdapter` on `EvalDb`;
the receipts go through the real `UserNotificationService` to a recording channel. Every assertion reads a stored
document, the delegation's response or a posted text.

Every script is `[tools(<call>), text("<summary>")]`: after any create/update/delete the agent makes a second,
tool-less request and only then returns (`notes_agent.py:466-480`), so a script of one item would run out.

Two defects are pinned as strict xfails, each beside a passing control:
- P14-D2: an unknown `complexity` on create is dropped, the reminder is stored without it and reported "created".
  Fix: return an error when `args.get("complexity")` is set and `_parse_complexity` gives None.
- P14-D3: an unparsable `due` on update is dropped and the update is reported done. Fix: after `_parse_dt`, return an
  error when `args.get("due")` is set and the result is None.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.agents.notes_agent import NotesAgent
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentConfig, AgentResponse, AgentStatus
from src.domain.agent_note import NoteCreate
from src.domain.task_complexity import TaskComplexity
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import NOTES, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.services.user_notification_service import UserNotificationService
from tests.support.fact_review_world import FakeNotificationState, RecordingChannelFactory
from tests.support.fact_lifecycle_world import NOW
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require
from tests.support.scripted_llm import ScriptedLLM, execution_context, text, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder
from tests.support.tool_fields import unused

USER = "u1"
ACCOUNT = "a1"
FILE = "projects/morning-briefing.md"
FAR_DUE = datetime(2030, 7, 1, 9, 0, tzinfo=timezone.utc)   # the seeds fire years from real now: listed as active

# --- the scripted calls (module constants, read by the tests and by the last test) ---
# A Wednesday, 07:00 Berlin; the rule says 08:00, so the stored fire time proves the snap onto the rule.
CREATE_ALL = {
    "text": "Springfield news every morning",
    "instruction": "Look up the news in Springfield and send a short digest.",
    "due": "2030-03-13T07:00:00",
    "recurrence": "FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
    "complexity": "deep_reasoning",
}
# The Thursday before a Friday rule: the stored fire time is the Friday.
UPDATE_ALL = {
    "note_id": "<the seeded note>",
    "text": "Weather in Springfield",
    "instruction": "Check the weather in Springfield and tell me if I need an umbrella.",
    "due": "2030-05-16T09:00:00",
    "recurrence": "FREQ=WEEKLY;BYDAY=FR",
    "complexity": "simple_analytics",
}
UPDATE_CLEAR = {"note_id": "<the seeded note>", "clear_recurrence": True}
DELETE = {"note_id": "<the seeded note>"}
CREATE_BAD_RULE = {"text": "Call dad", "instruction": "Remind me to call dad.", "due": "2030-03-13T07:00:00",
                   "recurrence": "FREQ=DAILY;COUNT=3"}
CREATE_BAD_COMPLEXITY = {"text": "Call dad", "instruction": "Remind me to call dad.", "due": "2030-03-13T07:00:00",
                         "complexity": "deep"}
UPDATE_BAD_DUE = {"note_id": "<the seeded note>", "due": "next tuesday 9am"}
# The delegate field (a `params` entry of the delegation, not a model argument).
PARAMS_BIND = {"workspace_file": FILE}
PARAMS_REMOVE = {"workspace_file": "remove"}


class Rig:
    """Notes behind a real coordinator, over the real note adapter, with a recording channel for the receipts."""

    def __init__(self, script: List[Any]) -> None:
        self.llm = ScriptedLLM(script)
        self.db = EvalDb()
        env = EnvironmentConfig()
        self.notes = FirestoreAgentNoteAdapter(self.db, env)
        self.state = FakeNotificationState(NOW)
        self.channels = RecordingChannelFactory()
        self.notifier = UserNotificationService(self.state, self.channels, coordinator=None, notification_sla={})
        registry = AgentRegistry()
        registry.register(NOTES)
        self.coordinator = AgentCoordinator(registry=registry)
        self.agent = NotesAgent(
            config=AgentConfig(agent_id=f"notes_agent_{USER}", agent_type="notes", timeout_ms=60_000,
                               capabilities=["note_management"]),
            execution_context=execution_context(self.llm, "notes"),
            notes_port=self.notes, recurrence=DateutilRecurrenceAdapter(),
            prompt_builder=StubPromptBuilder(), user_timezone="Europe/Berlin",
            notification_service=self.notifier, workspace_enabled=True,
        )
        self.coordinator.register_agent(self.agent)
        self.docs: Dict[str, Dict[str, Any]] = self.db.collection(env.orchestrator_notes_collection).docs

    async def start(self) -> "Rig":
        await self.state.save_primary(USER, "slack", "C1")
        return self

    async def seed(self, **over: Any) -> str:
        fields: Dict[str, Any] = dict(
            user_id=USER, text="Springfield news every morning",
            instruction="Look up the news in Springfield and send a short digest.", due=FAR_DUE,
            recurrence="FREQ=WEEKLY;BYDAY=MO", complexity=TaskComplexity.SMALL_TALK, workspace_file=FILE)
        fields.update(over)
        return (await self.notes.create_note(NoteCreate(**fields))).note_id

    async def delegate(self, params: Optional[Dict[str, Any]] = None,
                       query: str = "manage the reminder") -> AgentResponse:
        context: Dict[str, Any] = {"user_id": USER, "account_id": ACCOUNT}
        if params is not None:
            context["params"] = params
        return await self.coordinator.handle_delegation(
            intent=Intent.MANAGE_SELF_REMINDERS, query=query, context=context)

    def finished(self) -> bool:
        """Contract 5: every scripted answer was used and no request found the script empty."""
        return self.llm.unanswered == 0 and self.llm.pending() == 0

    def results_of(self, tool: str) -> List[Any]:
        return [result for name, result in tool_results(self.llm) if name == tool]

    @property
    def receipts(self) -> List[str]:
        return [t for _, _, t in self.channels.sent_literal]


def _call(template: Dict[str, Any], note_id: str) -> Dict[str, Any]:
    return {**template, "note_id": note_id}


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


async def test_create_with_every_field_and_a_workspace_file_is_stored():
    rig = await Rig([tools(("create_self_reminder", CREATE_ALL)), text("Reminder set.")]).start()

    response = await rig.delegate(PARAMS_BIND)

    assert response.status == AgentStatus.SUCCESS
    assert len(rig.docs) == 1
    doc = next(iter(rig.docs.values()))
    assert doc["user_id"] == USER
    assert doc["text"] == "Springfield news every morning"
    assert doc["instruction"] == "Look up the news in Springfield and send a short digest."
    # 07:00 Berlin (UTC+1 in March) was given; the rule's 08:00 is 07:00 UTC.
    assert doc["due"] == _utc(2030, 3, 13, 7, 0)
    assert doc["recurrence"] == "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"
    assert doc["complexity"] == "deep_reasoning"
    assert doc["workspace_file"] == FILE
    assert len(rig.receipts) == 1
    assert rig.receipts[0].startswith('📌 Reminder set: "Springfield news every morning" — 13 Mar 2030 08:00 CET')
    assert rig.receipts[0].endswith(" · workspace file projects/morning-briefing.md")
    assert rig.finished()


async def test_update_with_every_field_and_workspace_file_remove():
    rig = await Rig([
        tools(("update_self_reminder", UPDATE_ALL)), text("Updated."),
        tools(("update_self_reminder", UPDATE_CLEAR)), text("Now one-time."),
    ]).start()
    note_id = await rig.seed()
    rig.llm.script[0] = tools(("update_self_reminder", _call(UPDATE_ALL, note_id)))
    rig.llm.script[2] = tools(("update_self_reminder", _call(UPDATE_CLEAR, note_id)))
    before = dict(rig.docs[note_id])
    require(before["workspace_file"] == FILE and before["complexity"] == "small_talk"
            and before["recurrence"] == "FREQ=WEEKLY;BYDAY=MO", "the seed holds the fields the update changes")

    first = await rig.delegate(PARAMS_REMOVE)

    assert first.status == AgentStatus.SUCCESS
    doc = rig.docs[note_id]
    assert doc["text"] == "Weather in Springfield" != before["text"]
    assert doc["instruction"] == "Check the weather in Springfield and tell me if I need an umbrella."
    # Thursday 09:00 Berlin (CEST) snapped onto the Friday rule: Friday 09:00 Berlin = 07:00 UTC.
    assert doc["due"] == _utc(2030, 5, 17, 7, 0)
    assert doc["recurrence"] == "FREQ=WEEKLY;BYDAY=FR"
    assert doc["complexity"] == "simple_analytics"
    assert doc["workspace_file"] is None
    assert rig.receipts[-1].endswith(" · workspace file removed")

    second = await rig.delegate()

    assert second.status == AgentStatus.SUCCESS
    assert rig.docs[note_id]["recurrence"] is None
    assert rig.docs[note_id]["text"] == "Weather in Springfield"       # PATCH semantics: nothing else moved
    assert rig.docs[note_id]["due"] == _utc(2030, 5, 17, 7, 0)
    assert rig.finished()


async def test_delete_removes_the_note():
    rig = await Rig([tools(("delete_self_reminder", DELETE)), text("Deleted.")]).start()
    note_id = await rig.seed()
    rig.llm.script[0] = tools(("delete_self_reminder", _call(DELETE, note_id)))
    require(note_id in rig.docs, "the seed is stored")

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    assert rig.docs == {}
    assert [t for _, _, t in rig.channels.sent] == [f"🗑️ Reminder deleted: ID {note_id}"]   # not literal: no reminder words in it
    assert rig.finished()


async def test_an_invalid_rule_is_refused_and_nothing_is_stored():
    """Control: a refusal that is the code's. The script is used up, so it cannot be an exhausted model."""
    rig = await Rig([tools(("create_self_reminder", CREATE_BAD_RULE)), text("Could not set it.")]).start()

    response = await rig.delegate()

    assert response.status == AgentStatus.FAILED
    assert "Invalid recurrence rule" in (response.error or "")
    assert rig.docs == {}
    assert rig.receipts == []
    assert rig.finished()


async def test_a_known_complexity_is_stored_control():
    """Control for P14-D2: the same call with a declared value is stored and reported created."""
    rig = await Rig([tools(("create_self_reminder", {**CREATE_BAD_COMPLEXITY, "complexity": "small_talk"})),
                     text("Reminder set.")]).start()

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    assert [d["complexity"] for d in rig.docs.values()] == ["small_talk"]
    assert rig.finished()


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P14-D2: an unknown complexity is dropped by _parse_complexity and the reminder is stored and reported created")
async def test_an_unknown_complexity_is_refused_not_dropped():
    """Fix: `create_self_reminder` and `update_self_reminder` return an error when `args.get("complexity")` is set and
    `_parse_complexity` gives None (`notes_agent.py:232-238`, used `:533`, `:586`)."""
    rig = await Rig([tools(("create_self_reminder", CREATE_BAD_COMPLEXITY)), text("Reminder set.")]).start()

    response = await rig.delegate()

    require(rig.finished(), "every scripted answer was used and none was missing")
    require(len(rig.results_of("create_self_reminder")) == 1, "the model received the create's result")
    assert response.status == AgentStatus.FAILED and "complexity" in (response.error or "") and rig.docs == {}


async def test_a_parsable_due_on_update_is_stored_control():
    """Control for P14-D3: a due the parser reads moves the stored fire time."""
    rig = await Rig([tools(("update_self_reminder", UPDATE_BAD_DUE)), text("Updated.")]).start()
    note_id = await rig.seed(recurrence=None)
    rig.llm.script[0] = tools(("update_self_reminder", {"note_id": note_id, "due": "2030-09-02T09:00:00"}))

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    assert rig.docs[note_id]["due"] == _utc(2030, 9, 2, 7, 0)
    assert rig.finished()


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P14-D3: an unparsable due on update is dropped by _parse_dt and the update is reported done")
async def test_an_unparsable_due_on_update_is_refused_not_dropped():
    """Fix: `if args.get("due") and due is None: return {"error": ...}` after `notes_agent.py:565`."""
    rig = await Rig([tools(("update_self_reminder", UPDATE_BAD_DUE)), text("Updated.")]).start()
    note_id = await rig.seed(recurrence=None)
    rig.llm.script[0] = tools(("update_self_reminder", _call(UPDATE_BAD_DUE, note_id)))

    response = await rig.delegate()

    require(rig.finished(), "every scripted answer was used and none was missing")
    require(len(rig.results_of("update_self_reminder")) == 1, "the model received the update's result")
    require(rig.docs[note_id]["due"] == FAR_DUE, "the stored fire time did not move")
    assert response.status == AgentStatus.FAILED and "due" in (response.error or "")


def test_the_scripted_calls_use_every_declared_field():
    assert unused("notes", "create_self_reminder", [CREATE_ALL]) == []
    assert unused("notes", "update_self_reminder", [UPDATE_ALL, UPDATE_CLEAR]) == []
    assert unused("notes", "delete_self_reminder", [DELETE]) == []
    assert unused("delegate", Intent.MANAGE_SELF_REMINDERS, [PARAMS_BIND, PARAMS_REMOVE]) == []
