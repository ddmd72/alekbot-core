"""Tasks lookups: list first, one page answers a small list, and the run answers inside its time budget.

The real `TasksAgent` (its `_call_llm`, tool loop and dispatch) behind a scripted model, reached through a real
`AgentCoordinator`, over the real `MicrosoftToDoAdapter` on the aiohttp boundary fake and the real task index on
`EvalDb`. The agent's clock is injected (`monotonic=`), so a slow model is a clock step, not a sleep.

What is pinned:
- a lookup over a small list is one `list_tasks` call and one answer: two model turns, one Graph round trip (the list
  name and the tasks are read concurrently), and the result tells the model it has seen every task;
- a long list is paged (20 a page, dated soonest first, undated newest first) and says when more pages remain;
- `due_from` / `due_to` filter by due date (a date alone is the whole day); an unreadable date is an error, never a
  dropped filter;
- a search that finds nothing says another wording will not find more;
- once the budget (timeout minus the answer reserve) is spent no tool turn starts: one tool-less turn answers, and when
  even that cannot run or runs out of time the tool results are the answer, never a failure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from src.adapters.microsoft.todo_adapter import MicrosoftToDoAdapter
from src.agents import tasks_agent as tasks_module
from src.agents.tasks_agent import TasksAgent
from src.composition.connector_registry import ConnectorRegistry
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentConfig, AgentResponse, AgentStatus
from src.domain.llm import LLMRequest, LLMResponse
from src.infrastructure.agent_config import TASKS as TASKS_CFG
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import TASKS, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.services.task_indexing_service import TaskIndexingService
from tests.support import http_boundary_fake
from tests.support.fact_store import HashEmbedder
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.scripted_llm import RoutedLLM, ScriptedLLM, execution_context, text, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder

USER = "u1"
ACCOUNT = "a1"
GRAPH = "https://graph.microsoft.com/v1.0"
LIST_URL = f"{GRAPH}/me/todo/lists/l1"
TASKS_URL = f"{LIST_URL}/tasks"
BUDGET_S = TASKS_CFG.timeout_ms / 1000

LOOKUP = "Is the Rome trip in my tasks?"
# The owner's two open tasks (invented universe): neither is about the Rome trip.
CAR_SERVICE = {"id": "t1", "title": "Car service", "status": "notStarted", "importance": "normal",
               "body": {"content": "Bring the logbook", "contentType": "text"},
               "dueDateTime": {"dateTime": "2026-11-05T09:00:00.0000000", "timeZone": "UTC"},
               "createdDateTime": "2026-10-20T08:00:00Z"}
CALL_DAD = {"id": "t2", "title": "Call dad", "status": "notStarted", "importance": "normal",
            "createdDateTime": "2026-10-25T08:00:00Z"}


def _graph_task(n: int, due: Optional[str] = None, created: str = "2026-10-01T08:00:00Z") -> Dict[str, Any]:
    task: Dict[str, Any] = {"id": f"t{n}", "title": f"Garden chore {n}", "status": "notStarted",
                            "importance": "normal", "createdDateTime": created}
    if due:
        task["dueDateTime"] = {"dateTime": f"{due}.0000000", "timeZone": "UTC"}
    return task


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """Tasks behind a real coordinator over the real To Do adapter and task index; Graph holds `graph_tasks`."""

    def __init__(self, llm: Any, monkeypatch: Any, graph_tasks: List[Dict[str, Any]]) -> None:
        self.llm = llm
        self.clock = Clock()
        self.fake = http_boundary_fake.install(monkeypatch)
        self.fake.todo_empty_account()
        self.fake.route("GET", TASKS_URL, handler=lambda _r: (200, {"value": [dict(t) for t in graph_tasks]}))
        self.credentials = CredentialsStore()
        db = EvalDb()
        env = EnvironmentConfig()
        self.task_config = FirestoreTaskConfigRepository(db, env)
        self.provider = MicrosoftToDoAdapter(self.credentials.repo, self.task_config, "client-id", "client-secret",
                                             connector=ConnectorRegistry().get("microsoft_todo"))
        indexing = TaskIndexingService(HashEmbedder(), FirestoreTaskSearchIndex(db, env), self.provider)
        registry = AgentRegistry()
        registry.register(TASKS)
        self.coordinator = AgentCoordinator(registry=registry)
        self.agent = TasksAgent(
            config=AgentConfig(agent_id=f"tasks_agent_{USER}", agent_type="tasks", timeout_ms=TASKS_CFG.timeout_ms,
                               capabilities=["task_management"]),
            execution_context=execution_context(self.llm, "tasks"), prompt_builder=StubPromptBuilder(),
            tasks_provider=self.provider, task_indexing=indexing, user_id=USER, monotonic=self.clock)
        self.coordinator.register_agent(self.agent)

    async def start(self) -> "Rig":
        await self.credentials.repo.save(
            creds(USER, "microsoft_todo", token_expiry=datetime.now(timezone.utc) + timedelta(hours=1)))
        await self.task_config.set_primary_list_id_if_absent(USER, "l1")
        return self

    async def delegate(self, query: str = LOOKUP) -> AgentResponse:
        return await self.coordinator.handle_delegation(
            intent=Intent.MANAGE_USER_TASKS, query=query, context={"user_id": USER, "account_id": ACCOUNT})

    def graph_paths(self) -> List[str]:
        return sorted(r.url.split("?", 1)[0] for r in self.fake.requests)

    def results_of(self, tool: str) -> List[Any]:
        return [result for name, result in tool_results(self.llm) if name == tool]

    def finished(self) -> bool:
        return self.llm.unanswered == 0 and self.llm.pending() == 0


def _titles(result: Dict[str, Any]) -> List[str]:
    return [t["title"] for t in result["tasks"]]


# ---------------------------------------------------------------------------
# A lookup over a small list
# ---------------------------------------------------------------------------

async def test_a_lookup_with_no_match_is_one_list_call_and_one_answer(monkeypatch):
    answer = "No task is about the Rome trip. The 2 open tasks are Car service (due Nov 5) and Call dad."
    rig = await Rig(ScriptedLLM([tools(("list_tasks", {})), text(answer)]), monkeypatch,
                    [CALL_DAD, CAR_SERVICE]).start()

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    assert response.result == answer
    assert len(rig.llm.requests) == 2                                   # one tool turn, one answer
    assert [name for name, _ in tool_results(rig.llm)] == ["list_tasks"]
    (listed,) = rig.results_of("list_tasks")
    assert _titles(listed) == ["Car service", "Call dad"]               # dated first, then undated
    assert (listed["total"], listed["page"], listed["pages"]) == (2, 1, 1)
    assert listed["note"] == "These are all 2 open tasks: there is nothing more to list or search."
    assert listed["tasks"][0]["body"] == "Bring the logbook"
    assert all(len(t["ref"]) == 8 for t in listed["tasks"])
    assert rig.graph_paths() == [LIST_URL, TASKS_URL]                   # the list name and the tasks, nothing else
    assert rig.fake.unrouted == []
    assert rig.finished()


async def test_the_list_name_and_the_tasks_are_read_in_one_round_trip(monkeypatch):
    """The adapter starts the list-name GET and the tasks GET together: each waits until the other has started."""
    rig = await Rig(ScriptedLLM([]), monkeypatch, [CAR_SERVICE]).start()
    started = {"name": asyncio.Event(), "tasks": asyncio.Event()}
    real_get = rig.provider._get

    async def get(user_id: str, path: str) -> Dict[str, Any]:
        mine, other = ("tasks", "name") if path.startswith("/me/todo/lists/l1/tasks") else ("name", "tasks")
        started[mine].set()
        await asyncio.wait_for(started[other].wait(), timeout=1.0)   # sequential reads would time out here
        return await real_get(user_id, path)

    monkeypatch.setattr(rig.provider, "_get", get)

    tasks = await rig.provider.list_tasks(USER)

    assert [(t.title, t.list_name) for t in tasks] == [("Car service", "Alek Bot Tasks")]


# ---------------------------------------------------------------------------
# Paging, order and the due period
# ---------------------------------------------------------------------------

LONG_LIST = ([_graph_task(n, due=f"2026-12-{n:02d}T09:00:00") for n in range(1, 16)]
             + [_graph_task(n, created=f"2026-10-{n - 10:02d}T08:00:00Z") for n in range(16, 26)])


async def test_a_long_list_is_paged_and_says_when_more_remain(monkeypatch):
    rig = await Rig(ScriptedLLM([tools(("list_tasks", {})), tools(("list_tasks", {"page": 2})), text("Done.")]),
                    monkeypatch, list(reversed(LONG_LIST))).start()

    response = await rig.delegate("What do I have?")

    assert response.status == AgentStatus.SUCCESS
    first, second = rig.results_of("list_tasks")
    assert (first["count"], first["total"], first["page"], first["pages"]) == (20, 25, 1, 2)
    assert first["note"] == "Page 1 of 2: 25 open tasks. list_tasks with page=2 returns the next 20."
    # 15 dated tasks soonest first, then the undated ones newest first (created Oct 15 down to Oct 6).
    assert _titles(first) == [f"Garden chore {n}" for n in range(1, 16)] + [
        f"Garden chore {n}" for n in range(25, 20, -1)]
    assert _titles(second) == [f"Garden chore {n}" for n in range(20, 15, -1)]
    assert second["note"] == "Page 2 of 2, the last: 25 open tasks."
    assert rig.finished()


async def test_a_due_period_keeps_the_tasks_due_in_it_and_a_date_alone_is_the_whole_day(monkeypatch):
    rig = await Rig(ScriptedLLM([tools(("list_tasks", {"due_from": "2026-12-03", "due_to": "2026-12-05"})),
                                 text("Three tasks.")]), monkeypatch, LONG_LIST).start()

    await rig.delegate("What is due Dec 3 to Dec 5?")

    (listed,) = rig.results_of("list_tasks")
    assert _titles(listed) == ["Garden chore 3", "Garden chore 4", "Garden chore 5"]   # Dec 5 09:00 is inside
    assert listed["note"] == "These are all 3 open tasks due in that period: there is nothing more to list or search."
    assert rig.finished()


async def test_an_unreadable_due_date_is_an_error_not_a_dropped_filter(monkeypatch):
    rig = await Rig(ScriptedLLM([tools(("list_tasks", {"due_from": "next Friday"})), text("Could not read it.")]),
                    monkeypatch, LONG_LIST).start()

    await rig.delegate("What is due from next Friday?")

    (listed,) = rig.results_of("list_tasks")
    assert listed == {"error": "due_from is not an ISO-8601 date or date-time: 'next Friday'"}
    assert rig.finished()


async def test_a_page_past_the_last_says_so(monkeypatch):
    rig = await Rig(ScriptedLLM([tools(("list_tasks", {"page": 3})), text("Nothing there.")]), monkeypatch,
                    [CAR_SERVICE, CALL_DAD]).start()

    await rig.delegate("What do I have?")

    (listed,) = rig.results_of("list_tasks")
    assert listed["tasks"] == [] and listed["note"] == "Page 3 is past the last page (1) of the 2 open tasks."
    assert rig.finished()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def test_a_search_that_finds_nothing_says_other_words_will_not_find_more(monkeypatch):
    rig = await Rig(ScriptedLLM([tools(("search_tasks", {"query": "Rome trip"})), text("No matching task.")]),
                    monkeypatch, [CAR_SERVICE, CALL_DAD]).start()

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    (found,) = rig.results_of("search_tasks")
    assert found["tasks"] == [] and found["count"] == 0
    assert "Another wording of the same topic will not find more" in found["note"]
    assert "'Rome trip'" in found["note"]
    assert rig.finished()


def test_the_tool_texts_send_lookups_to_list_tasks_and_search_to_long_lists_only():
    by_name = {t["name"]: t["description"] for t in tasks_module._TOOL_DECLARATIONS}
    assert by_name["list_tasks"].startswith("The first tool for every lookup")
    assert "for a long task list only" in by_name["search_tasks"]
    assert "One search per topic" in by_name["search_tasks"]


# ---------------------------------------------------------------------------
# The time budget
# ---------------------------------------------------------------------------

def _stepping(rig_ref: List[Rig], replies: List[LLMResponse], steps: List[float]) -> RoutedLLM:
    """A model whose n-th answer is `replies[n]` and which moves the agent's clock by `steps[n]` seconds."""
    def handle(_request: LLMRequest) -> Optional[LLMResponse]:
        n = len(model.requests) - 1
        if n >= len(replies):
            return None
        rig_ref[0].clock.now += steps[n]
        return replies[n]
    model = RoutedLLM(handle, pending=lambda: len(replies) - len(model.requests))
    return model


async def test_when_the_budget_is_spent_no_tool_turn_starts_and_one_turn_answers(monkeypatch):
    ref: List[Rig] = []
    llm = _stepping(ref, [tools(("list_tasks", {})), tools(("list_tasks", {"page": 2})), text("Car service only.")],
                    [12.0, 12.0, 1.0])
    rig = await Rig(llm, monkeypatch, [CAR_SERVICE]).start()
    ref.append(rig)

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    assert response.result == "Car service only."
    assert len(llm.requests) == 3            # two tool turns (24 s of 30), then no third tool turn
    final = llm.requests[-1]
    assert final.tools == []
    assert final.messages[-1].parts[0].text == tasks_module._FORCE_ANSWER
    assert rig.finished()


async def test_with_no_time_for_a_final_turn_the_tool_results_are_the_answer(monkeypatch):
    ref: List[Rig] = []
    llm = _stepping(ref, [tools(("list_tasks", {}))], [BUDGET_S])
    rig = await Rig(llm, monkeypatch, [CAR_SERVICE, CALL_DAD]).start()
    ref.append(rig)

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    assert len(llm.requests) == 1
    assert response.result.startswith("Tool note: the task lookup reached its time limit")
    assert "Car service" in response.result and "Call dad" in response.result
    assert rig.finished()


async def test_a_final_turn_that_runs_out_of_time_still_returns_the_tool_results(monkeypatch):
    """The final turn gets only the time left before the margin (here 0.05 s of real time) and is cut there."""
    ref: List[Rig] = []
    hung: List[bool] = []

    async def handle(request: LLMRequest) -> Optional[LLMResponse]:
        if len(llm.requests) == 1:
            ref[0].clock.now += BUDGET_S - tasks_module._FINAL_MARGIN_S - 0.05
            return tools(("list_tasks", {}))
        hung.append(True)
        await asyncio.sleep(5)
        return text("too late")

    llm = RoutedLLM(handle)
    rig = await Rig(llm, monkeypatch, [CAR_SERVICE]).start()
    ref.append(rig)

    response = await rig.delegate()

    assert response.status == AgentStatus.SUCCESS
    assert hung == [True] and llm.requests[-1].tools == []
    assert response.result.startswith("Tool note:") and "Car service" in response.result
    assert llm.unanswered == 0
