"""Tasks, the time fields a model sends, read where Graph receives them (P14-D5, P16-D11 follow-ups).

The real `TasksAgent` behind a scripted `LLMPort`, over the real `MicrosoftToDoAdapter` on the aiohttp boundary fake
(`tests/support/http_boundary_fake.py`) and the real `TaskIndexingService` over `FirestoreTaskSearchIndex` on `EvalDb`.
Assertions read the request bodies Graph received and the tool results the model was given.

- An unparsable time in any field of create_task / update_task is the tool's error, and nothing reaches Graph
  (the pinned case, an unparsable due on create, lives in `test_tasks_agent_tool_fields_e2e.py`).
- The automatic reminder is 20:00 of the user's own zone on the evening before the due date (owner decision O7(b)).
"""
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from src.adapters.microsoft.todo_adapter import MicrosoftToDoAdapter
from src.agents.tasks_agent import TasksAgent
from src.composition.connector_registry import ConnectorRegistry
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.task import Task, TaskCreate
from src.services.task_indexing_service import TaskIndexingService
from tests.support import http_boundary_fake
from tests.support.clock_instants import BERLIN, HELSINKI, NEW_YORK, transition
from tests.support.fact_store import HashEmbedder
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.redelivery_world import require
from tests.support.scripted_llm import ScriptedLLM, execution_context, text, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder

USER = "u1"
GRAPH = "https://graph.microsoft.com/v1.0"
TASKS_URL = f"{GRAPH}/me/todo/lists/l1/tasks"
REF = hashlib.md5(b"t1").hexdigest()[:8]    # the `task_ref` of the first task the fake stores


def _instant(payload: Dict[str, str]) -> datetime:
    """A Graph dateTimeTimeZone read as Graph reads it: the wall-clock in the named zone (the adapter sends UTC)."""
    require(payload["timeZone"] == "UTC", "the adapter labels every time UTC")
    return datetime.fromisoformat(payload["dateTime"]).replace(tzinfo=timezone.utc)


class Rig:
    """One TasksAgent of a user in `zone`, over the real To Do adapter (Graph boundary fake) and the real index."""

    def __init__(self, script: List[Any], monkeypatch: Any, zone: str = "UTC") -> None:
        self.llm = ScriptedLLM(script)
        self.fake = http_boundary_fake.install(monkeypatch)
        self.fake.todo_empty_account()
        self.graph: Dict[str, Dict[str, Any]] = {}
        self._route_tasks()
        self.credentials = CredentialsStore()
        db = EvalDb()
        env = EnvironmentConfig()
        self.provider = MicrosoftToDoAdapter(
            self.credentials.repo, FirestoreTaskConfigRepository(db, env), "client-id", "client-secret",
            connector=ConnectorRegistry().get("microsoft_todo"))
        self.indexing = TaskIndexingService(HashEmbedder(), FirestoreTaskSearchIndex(db, env), self.provider)
        self.agent = TasksAgent(
            config=AgentConfig(agent_id=f"tasks_agent_{USER}", agent_type="tasks", timeout_ms=60_000,
                               capabilities=["task_management"]),
            execution_context=execution_context(self.llm, "tasks"), prompt_builder=StubPromptBuilder(),
            tasks_provider=self.provider, task_indexing=self.indexing, user_id=USER, user_timezone=zone)
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

        self.fake.route("POST", TASKS_URL, handler=create)
        self.fake.route("GET", f"{TASKS_URL}/", handler=read, prefix=True)
        self.fake.route("PATCH", f"{TASKS_URL}/", handler=patch, prefix=True)

    async def start(self) -> "Rig":
        await self.credentials.repo.save(
            creds(USER, "microsoft_todo", token_expiry=datetime.now(timezone.utc) + timedelta(hours=1)))
        return self

    async def seed(self, **fields: Any) -> Task:
        """A task stored in Graph and indexed; later requests are counted from here (`sent`)."""
        task = await self.provider.create_task(USER, TaskCreate(**{"title": "Car service", **fields}))
        await self.indexing.index_task(task)
        self._seen = len(self.fake.requests)
        return task

    async def run(self) -> None:
        await self.agent.execute(AgentMessage(
            task_id="task-1", sender="smart", recipient=self.agent.agent_id, intent=AgentIntent.QUERY,
            payload={"query": "manage the task"}, context={"user_id": USER, "account_id": "a1"}))

    def sent(self, method: str, url_prefix: str) -> List[Any]:
        return [r for r in self.fake.requests[self._seen:] if r.method == method and r.url.startswith(url_prefix)]

    def results_of(self, tool: str) -> List[Any]:
        return [result for name, result in tool_results(self.llm) if name == tool]

    def finished(self) -> bool:
        return self.llm.unanswered == 0 and self.llm.pending() == 0


# ------------------------------------------------------------------------------------- an unparsable time (P14-D5)

async def test_an_unparsable_start_or_reminder_on_create_is_refused(monkeypatch):
    rig = await Rig([tools(("create_task", {"title": "Car service", "start_datetime": "tomorrow"}),
                           ("create_task", {"title": "Car service", "reminder_datetime": "at eight"})),
                     text("Could not create it.")], monkeypatch).start()

    await rig.run()

    results = rig.results_of("create_task")
    require(rig.finished() and len(results) == 2, "the model received both results")
    assert rig.sent("POST", TASKS_URL) == []
    assert "error" in results[0] and "'tomorrow'" in results[0]["error"]
    assert "error" in results[1] and "'at eight'" in results[1]["error"]


async def test_an_unparsable_due_or_reminder_on_update_is_refused(monkeypatch):
    rig = await Rig([tools(("update_task", {"task_ref": REF, "due_datetime": "next Friday"}),
                           ("update_task", {"task_ref": REF, "reminder_datetime": "evening"})),
                     text("Could not change it.")], monkeypatch).start()
    await rig.seed()

    await rig.run()

    results = rig.results_of("update_task")
    require(rig.finished() and len(results) == 2, "the model received both results")
    assert rig.sent("PATCH", f"{TASKS_URL}/") == []
    assert all("error" in r for r in results)
    assert rig.graph["t1"].get("dueDateTime") is None


async def test_an_unparsable_due_with_a_recurrence_is_refused(monkeypatch):
    """The recurrence reads the due for its defaults: an unparsable due is refused there too, not replaced by now."""
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "next Friday",
                                            "recurrence": {"pattern": "weekly"}})),
                     text("Could not create it.")], monkeypatch).start()

    await rig.run()

    (result,) = rig.results_of("create_task")
    assert rig.sent("POST", TASKS_URL) == [] and "error" in result


# ------------------------------------------------------------- the automatic reminder in the user's zone (P16-D11)

def _evening_before(zone: str, due_day: str) -> datetime:
    """20:00 in `zone` on the day before `due_day` (YYYY-MM-DD), as an aware UTC instant."""
    day = datetime.fromisoformat(due_day) - timedelta(days=1)
    return day.replace(hour=20, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)


async def test_a_date_only_due_gets_its_reminder_at_20_local_the_evening_before(monkeypatch):
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04"})),
                     text("Created.")], monkeypatch, zone=BERLIN).start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    require(rig.finished(), "every scripted answer was used")
    assert post.json["isReminderOn"] is True
    assert _instant(post.json["reminderDateTime"]) == _evening_before(BERLIN, "2026-11-04")   # 19:00Z


async def test_the_evening_before_is_a_local_evening_even_when_it_is_the_next_utc_day(monkeypatch):
    """New York, the evening of Nov 3 (EST) is 01:00Z on Nov 4: the reminder is still the local evening before."""
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04T09:00:00"})),
                     text("Created.")], monkeypatch, zone=NEW_YORK).start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    assert _instant(post.json["reminderDateTime"]) == datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)


async def test_an_update_that_gives_a_due_sets_the_reminder_in_the_users_zone(monkeypatch):
    rig = await Rig([tools(("update_task", {"task_ref": REF, "due_datetime": "2026-11-04"})), text("Updated.")],
                    monkeypatch, zone=HELSINKI).start()
    await rig.seed()

    await rig.run()

    (patch,) = [r.json for r in rig.sent("PATCH", f"{TASKS_URL}/")]
    assert patch["isReminderOn"] is True
    assert _instant(patch["reminderDateTime"]) == _evening_before(HELSINKI, "2026-11-04")   # 18:00Z


async def test_an_aware_due_is_dated_in_the_users_zone(monkeypatch):
    """00:30Z on Nov 4 is 19:30 on Nov 3 in New York: the due day there is Nov 3, the reminder Nov 2, 20:00 local."""
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04T00:30:00+00:00"})),
                     text("Created.")], monkeypatch, zone=NEW_YORK).start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    assert _instant(post.json["reminderDateTime"]) == _evening_before(NEW_YORK, "2026-11-03")


async def test_the_evening_before_takes_the_offset_of_its_own_side_of_a_dst_change(monkeypatch):
    """Berlin leaves summer time on Oct 25: the evening of Oct 24 is CEST (18:00Z), the evening of Oct 25 CET (19:00Z)."""
    fall = transition(BERLIN, "fall", "T").utc
    require(fall.date().isoformat() == "2026-10-25", "the Berlin fall transition is on Oct 25")
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-10-25"}),
                           ("create_task", {"title": "Car service", "due_datetime": "2026-10-26"})),
                     text("Created.")], monkeypatch, zone=BERLIN).start()

    await rig.run()

    first, second = [p.json for p in rig.sent("POST", TASKS_URL)]
    assert _instant(first["reminderDateTime"]) == datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
    assert _instant(second["reminderDateTime"]) == datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)


async def test_an_unknown_zone_reads_as_utc(monkeypatch):
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04"})),
                     text("Created.")], monkeypatch, zone="Mars/Olympus_Mons").start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    assert _instant(post.json["reminderDateTime"]) == datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)


async def test_an_explicit_reminder_is_kept_whatever_the_zone(monkeypatch):
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04",
                                            "reminder_datetime": "2026-11-03T07:00:00Z"})),
                     text("Created.")], monkeypatch, zone=BERLIN).start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    assert _instant(post.json["reminderDateTime"]) == datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
