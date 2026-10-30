"""Tasks, every declared field to Graph and the task index (TEST_COVERAGE_SYSTEMIC_RFC §5 P14, card E).

The real `TasksAgent` (its own `_call_llm`, tool loop and dispatch) behind a scripted `LLMPort`, reached the way Smart
reaches it: `AgentCoordinator.handle_delegation` over a registry holding the Tasks descriptor, so the delegate field
`background` arrives through the `params` spread. The provider is the real `MicrosoftToDoAdapter` over the aiohttp
boundary fake (`tests/support/http_boundary_fake.py`) with its own task routes; the index is the real
`TaskIndexingService` over `FirestoreTaskSearchIndex` on `EvalDb`. Every assertion reads a request body Graph received,
a stored index document or a tool result the model was given.

Two defects are pinned as strict xfails, each beside a passing control:
- P14-D4: an explicit `is_reminder_on: false` is overwritten when the update also sets a due date on a task that had
  none. Fix: `and is_reminder_on is not False` at the auto-reminder branch of `update_task` (`tasks_agent.py:528`).
- P14-D5: an unparsable `due_datetime` is dropped with a log line and the task is created without a due date. Fix:
  `_parse_datetime` (`tasks_agent.py:712-720`) raises `ValueError` on a non-empty value it cannot read; `execute`
  turns the raise into `{"error": ...}` before the POST.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from src.adapters.microsoft.todo_adapter import MicrosoftToDoAdapter
from src.agents.tasks_agent import TasksAgent
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentConfig, AgentResponse, AgentStatus
from src.domain.task import Task, TaskCreate
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import TASKS, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.services.task_indexing_service import TaskIndexingService
from tests.support import http_boundary_fake
from tests.support.fact_store import HashEmbedder
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.redelivery_world import require
from tests.support.scripted_llm import ScriptedLLM, execution_context, first_user_text, text, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder
from tests.support.tool_fields import unused
from src.composition.connector_registry import ConnectorRegistry

USER = "u1"
ACCOUNT = "a1"
GRAPH = "https://graph.microsoft.com/v1.0"
TASKS_URL = f"{GRAPH}/me/todo/lists/l1/tasks"
LISTS_URL = f"{GRAPH}/me/todo/lists"
FIRST_TASK = "t1"            # the id the fake gives the first task it stores
REF = hashlib.md5(FIRST_TASK.encode()).hexdigest()[:8]    # the 8-char `task_ref` of that task

# --- the scripted calls (module constants, read by the tests and by the last test) ---
CREATE_YEARLY = {
    "title": "Car service", "body": "Bring the logbook",
    "due_datetime": "2026-11-04T09:00:00", "start_datetime": "2026-11-03T09:00:00",
    "reminder_datetime": "2026-11-03T18:00:00", "importance": "high", "tags": ["car", "service"],
    "recurrence": {"pattern": "absoluteYearly", "interval": 1, "day_of_month": 4, "month": 11},
}
CREATE_WEEKLY = {
    "title": "Car service",
    "recurrence": {"pattern": "weekly", "interval": 2, "days_of_week": ["tuesday"]},
}
UPDATE_WEEKLY = {
    "task_ref": REF, "title": "Car service Nov 4", "body": "Bring the logbook",
    "due_datetime": "2026-11-04T09:00:00", "start_datetime": "2026-11-03T09:00:00",
    "reminder_datetime": "2026-11-03T18:00:00", "is_reminder_on": True, "importance": "low",
    "status": "completed", "tags": ["car", "garage"],
    "recurrence": {"pattern": "weekly", "interval": 2, "days_of_week": ["monday", "friday"]},
}
UPDATE_MONTHLY = {"task_ref": REF, "recurrence": {"pattern": "absoluteMonthly", "day_of_month": 4}}
UPDATE_YEARLY = {"task_ref": REF, "recurrence": {"pattern": "absoluteYearly", "day_of_month": 4, "month": 11}}
DELETE = {"task_ref": REF}
UPDATE_NEW_DUE = {"task_ref": REF, "due_datetime": "2026-11-04T09:00:00"}
UPDATE_NEW_DUE_REMINDER_OFF = {**UPDATE_NEW_DUE, "is_reminder_on": False}
CREATE_BAD_DUE = {"title": "Car service", "due_datetime": "next Friday"}
# The delegate field (a `params` entry of the delegation, not a model argument).
PARAMS_BACKGROUND = {"background": "The car service is on Nov 4, bring the logbook."}


def _utc_payload(value: str) -> Dict[str, str]:
    return {"dateTime": value, "timeZone": "UTC"}


class Rig:
    """Tasks behind a real coordinator, over the real To Do adapter (Graph boundary fake) and the real task index."""

    def __init__(self, script: List[Any], monkeypatch: Any) -> None:
        self.llm = ScriptedLLM(script)
        self.fake = http_boundary_fake.install(monkeypatch)
        self.fake.todo_empty_account()
        self.graph: Dict[str, Dict[str, Any]] = {}
        self._route_tasks()
        self.credentials = CredentialsStore()
        self.db = EvalDb()
        env = EnvironmentConfig()
        self.index_docs: Dict[str, Dict[str, Any]] = self.db.collection(env.task_search_index_collection).docs
        self.provider = MicrosoftToDoAdapter(
            self.credentials.repo, FirestoreTaskConfigRepository(self.db, env), "client-id", "client-secret", connector=ConnectorRegistry().get("microsoft_todo"))
        self.indexing = TaskIndexingService(HashEmbedder(), FirestoreTaskSearchIndex(self.db, env), self.provider)
        registry = AgentRegistry()
        registry.register(TASKS)
        self.coordinator = AgentCoordinator(registry=registry)
        self.agent = TasksAgent(
            config=AgentConfig(agent_id=f"tasks_agent_{USER}", agent_type="tasks", timeout_ms=60_000,
                               capabilities=["task_management"]),
            execution_context=execution_context(self.llm, "tasks"), prompt_builder=StubPromptBuilder(),
            tasks_provider=self.provider, task_indexing=self.indexing, user_id=USER)
        self.coordinator.register_agent(self.agent)
        self._seen = 0

    def _route_tasks(self) -> None:
        def task_id(url: str) -> str:
            return url.split("?", 1)[0].rsplit("/", 1)[1]

        def create(req: Any) -> Any:
            stored = {"id": f"t{len(self.graph) + 1}", **req.json}
            self.graph[stored["id"]] = stored
            return 201, dict(stored)

        def read(req: Any) -> Any:
            return 200, dict(self.graph[task_id(req.url)])

        def patch(req: Any) -> Any:
            self.graph[task_id(req.url)].update(req.json)
            return 200, dict(self.graph[task_id(req.url)])

        def delete(req: Any) -> Any:
            self.graph.pop(task_id(req.url), None)
            return 204, None

        self.fake.route("POST", TASKS_URL, handler=create)
        self.fake.route("GET", f"{TASKS_URL}/", handler=read, prefix=True)
        self.fake.route("PATCH", f"{TASKS_URL}/", handler=patch, prefix=True)
        self.fake.route("DELETE", f"{TASKS_URL}/", handler=delete, prefix=True)

    async def start(self) -> "Rig":
        await self.credentials.repo.save(
            creds(USER, "microsoft_todo", token_expiry=datetime.now(timezone.utc) + timedelta(hours=1)))
        return self

    async def seed(self, **fields: Any) -> Task:
        """A task stored in Graph and indexed, made through the real provider and indexing service; later requests are
        counted from here (`sent`)."""
        task = await self.provider.create_task(USER, TaskCreate(**{"title": "Car service", **fields}))
        await self.indexing.index_task(task)
        self._seen = len(self.fake.requests)
        return task

    async def delegate(self, params: Optional[Dict[str, Any]] = None, query: str = "manage the task") -> AgentResponse:
        context: Dict[str, Any] = {"user_id": USER, "account_id": ACCOUNT}
        if params is not None:
            context["params"] = params
        return await self.coordinator.handle_delegation(intent=Intent.MANAGE_USER_TASKS, query=query, context=context)

    def sent(self, method: str, url_prefix: str) -> List[Any]:
        """Requests of `method` to a URL starting with `url_prefix`, after the seed."""
        return [r for r in self.fake.requests[self._seen:] if r.method == method and r.url.startswith(url_prefix)]

    def finished(self) -> bool:
        """Contract 5: every scripted answer was used and no request found the script empty."""
        return self.llm.unanswered == 0 and self.llm.pending() == 0

    def results_of(self, tool: str) -> List[Any]:
        return [result for name, result in tool_results(self.llm) if name == tool]


async def test_create_with_every_field_reaches_graph_and_the_index(monkeypatch):
    rig = await Rig([tools(("create_task", CREATE_YEARLY), ("create_task", CREATE_WEEKLY)), text("Created.")],
                    monkeypatch).start()

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    posts = rig.sent("POST", TASKS_URL)
    assert len(posts) == 2
    yearly, weekly = posts[0].json, posts[1].json
    assert yearly["title"] == "Car service"
    assert yearly["body"] == {"content": "Bring the logbook", "contentType": "text"}
    assert yearly["dueDateTime"] == _utc_payload("2026-11-04T09:00:00")
    assert yearly["startDateTime"] == _utc_payload("2026-11-03T09:00:00")
    assert yearly["reminderDateTime"] == _utc_payload("2026-11-03T18:00:00")
    assert yearly["importance"] == "high"
    assert yearly["categories"] == ["car", "service"]
    assert yearly["isReminderOn"] is True
    assert yearly["recurrence"]["pattern"] == {
        "type": "absoluteYearly", "interval": 1, "firstDayOfWeek": "sunday", "dayOfMonth": 4, "month": 11}
    assert yearly["recurrence"]["range"]["type"] == "noEnd"
    assert weekly["recurrence"]["pattern"] == {
        "type": "weekly", "interval": 2, "firstDayOfWeek": "sunday", "daysOfWeek": ["tuesday"]}
    assert weekly["isReminderOn"] is False and "dueDateTime" not in weekly
    first = rig.index_docs[f"{USER}_t1"]
    assert (first["title"], first["tags"], first["importance"], first["short_id"]) == (
        "Car service", ["car", "service"], "high", REF)
    assert first["status"] == "notStarted"
    assert rig.index_docs[f"{USER}_t2"]["short_id"] == hashlib.md5(b"t2").hexdigest()[:8]
    assert rig.results_of("create_task") == [{"created": True, "title": "Car service"}] * 2
    assert rig.fake.unrouted == []
    assert rig.finished()


async def test_update_with_every_field_reaches_graph_and_the_index(monkeypatch):
    rig = await Rig([tools(("update_task", UPDATE_WEEKLY), ("update_task", UPDATE_MONTHLY),
                           ("update_task", UPDATE_YEARLY)), text("Updated.")], monkeypatch).start()
    await rig.seed(importance="normal")
    require(REF == hashlib.md5(rig.index_docs[f"{USER}_t1"]["task_id"].encode()).hexdigest()[:8],
            "the seed is the task the constants' ref names")

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    weekly, monthly, yearly = [r.json for r in rig.sent("PATCH", f"{TASKS_URL}/{FIRST_TASK}")]
    assert weekly["title"] == "Car service Nov 4"
    assert weekly["body"] == {"content": "Bring the logbook", "contentType": "text"}
    assert weekly["dueDateTime"] == _utc_payload("2026-11-04T09:00:00")
    assert weekly["startDateTime"] == _utc_payload("2026-11-03T09:00:00")
    assert weekly["reminderDateTime"] == _utc_payload("2026-11-03T18:00:00")
    assert weekly["isReminderOn"] is True
    assert weekly["importance"] == "low"
    assert weekly["status"] == "completed"
    assert weekly["categories"] == ["car", "garage"]
    assert weekly["recurrence"]["pattern"] == {
        "type": "weekly", "interval": 2, "firstDayOfWeek": "sunday", "daysOfWeek": ["monday", "friday"]}
    assert set(monthly) == {"recurrence"}
    assert monthly["recurrence"]["pattern"] == {
        "type": "absoluteMonthly", "interval": 1, "firstDayOfWeek": "sunday", "dayOfMonth": 4}
    assert set(yearly) == {"recurrence"}
    assert yearly["recurrence"]["pattern"] == {
        "type": "absoluteYearly", "interval": 1, "firstDayOfWeek": "sunday", "dayOfMonth": 4, "month": 11}
    indexed = rig.index_docs[f"{USER}_t1"]
    assert (indexed["status"], indexed["tags"], indexed["importance"]) == ("completed", ["car", "garage"], "low")
    assert rig.results_of("update_task") == [{"updated": True, "title": "Car service Nov 4"}] * 3
    assert rig.fake.unrouted == []
    assert rig.finished()


async def test_delete_removes_the_task_and_its_index_entry(monkeypatch):
    rig = await Rig([tools(("delete_task", DELETE)), text("Deleted.")], monkeypatch).start()
    await rig.seed()
    require(f"{USER}_t1" in rig.index_docs and FIRST_TASK in rig.graph, "the seed is in Graph and in the index")

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    assert len(rig.sent("DELETE", f"{TASKS_URL}/{FIRST_TASK}")) == 1
    assert rig.graph == {}
    assert rig.index_docs == {}
    assert rig.results_of("delete_task") == [{"deleted": True}]
    assert rig.fake.unrouted == []
    assert rig.finished()


async def test_background_reaches_the_task_agents_prompt(monkeypatch):
    """A prompt input, not a stored value: the delegate field is appended to the agent's first user message."""
    rig = await Rig([text("Nothing to change.")], monkeypatch).start()

    response = await rig.delegate(PARAMS_BACKGROUND, query="what is on my list for the car service?")

    assert response.status == AgentStatus.SUCCESS
    assert first_user_text(rig.llm).endswith("\n\nContext: The car service is on Nov 4, bring the logbook.")
    assert rig.fake.unrouted == []
    assert rig.finished()


async def test_a_new_due_turns_the_reminder_on_the_evening_before_control(monkeypatch):
    """Control for P14-D4: the same update without `is_reminder_on` sets the auto reminder (20:00 the day before).
    The zone defect P16-D11 is not this test's."""
    rig = await Rig([tools(("update_task", UPDATE_NEW_DUE)), text("Updated.")], monkeypatch).start()
    await rig.seed()

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    (patch,) = [r.json for r in rig.sent("PATCH", f"{TASKS_URL}/{FIRST_TASK}")]
    assert patch["dueDateTime"] == _utc_payload("2026-11-04T09:00:00")
    assert patch["isReminderOn"] is True
    assert patch["reminderDateTime"] == _utc_payload("2026-11-03T20:00:00")
    assert rig.fake.unrouted == []
    assert rig.finished()


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P14-D4: the auto-reminder branch overwrites an explicit is_reminder_on=false")
async def test_an_explicit_reminder_off_survives_a_new_due(monkeypatch):
    """Fix: `and is_reminder_on is not False` on the auto-reminder condition (`tasks_agent.py:528`)."""
    rig = await Rig([tools(("update_task", UPDATE_NEW_DUE_REMINDER_OFF)), text("Updated.")], monkeypatch).start()
    await rig.seed()

    await rig.delegate()

    patches = rig.sent("PATCH", f"{TASKS_URL}/{FIRST_TASK}")
    require(rig.finished(), "every scripted answer was used and none was missing")
    require(len(patches) == 1, "exactly one PATCH reached the task")
    require(rig.fake.unrouted == [], "every Graph request was routed")
    assert patches[0].json["isReminderOn"] is False


async def test_a_parsable_due_creates_the_task_control(monkeypatch):
    """Control for P14-D5: the same call with a due the parser reads reaches Graph with that due."""
    rig = await Rig([tools(("create_task", {**CREATE_BAD_DUE, "due_datetime": "2026-11-04T09:00:00"})),
                     text("Created.")], monkeypatch).start()

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    (post,) = rig.sent("POST", TASKS_URL)
    assert post.json["dueDateTime"] == _utc_payload("2026-11-04T09:00:00")
    assert rig.fake.unrouted == []
    assert rig.finished()


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P14-D5: an unparsable due_datetime is dropped by _parse_datetime and the task is created without it")
async def test_an_unparsable_due_is_refused_not_dropped(monkeypatch):
    """Fix: `_parse_datetime` raises `ValueError` on a non-empty unparsable value (`tasks_agent.py:712-720`); it is raised
    inside `_execute_tool`, before the POST, and the loop turns it into `{"error": ...}` (`:422-426`)."""
    rig = await Rig([tools(("create_task", CREATE_BAD_DUE)), text("Created.")], monkeypatch).start()

    await rig.delegate()

    results = rig.results_of("create_task")
    require(rig.finished(), "every scripted answer was used and none was missing")
    require(len(results) == 1, "the model received the create's result")
    require(rig.fake.unrouted == [], "every Graph request was routed")
    # Only the task URL: the list-creation POST (to `.../me/todo/lists`) is not a task.
    assert rig.sent("POST", TASKS_URL) == [] and "error" in results[0]


def test_the_scripted_calls_use_every_declared_field():
    assert unused("tasks", "create_task", [CREATE_YEARLY, CREATE_WEEKLY]) == []
    assert unused("tasks", "update_task", [UPDATE_WEEKLY, UPDATE_MONTHLY, UPDATE_YEARLY]) == []
    assert unused("tasks", "delete_task", [DELETE]) == []
    assert unused("delegate", Intent.MANAGE_USER_TASKS, [PARAMS_BACKGROUND]) == []
