"""The drive specialist reads passages end to end (USER_DRIVE_INDEXING_RFC §28.3 A2, A3, A5, A7, A11, A14; plan
S2.2): the real `DriveAgent` with a scripted model, its real `DriveToolRun` handlers, the user's real
`DriveOperations` and the real `DriveSearchService` over the in-memory index repository (real cosine; the question's
and the chunks' vectors set by hand, as the service's own passages tests do). A chat turn finds the passages in a
folder, reads the neighbours by number and reports; a reminder run that read passages has its later move refused.
The drive service is a spec mock: the only thing asserted on it is that no change reached it. Invented universe only."""
from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock, MagicMock

from src.agents.drive_agent import DriveAgent
from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import PROMPT_CACHE_BOUNDARY
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.drive_operations import DriveOperations
from src.ports.llm_port import AgentExecutionContext
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.scripted_llm import calls, script
from tests.unit.services.drive_harness_connections import harness_connections
from tests.unit.services.drive_search_v2_fixture import V2World

QUESTION = "What does Tom's plan say happens after the first review?"
PLAN_TEXTS = [
    "Tom's plan for the Acme R&D review, draft two.",
    "Scope: the review covers the specifications in R&D.",
    "The first review is held by Lukas Weber in week one.",
    "After the first review, Acme signs off the specification.",
    "Then the budget for the second review phase is approved.",
    "The second review closes Tom's plan.",
    "Each review has minutes in the R&D folder.",
    "Tom Baker keeps the plan's review dates.",
    # four chunks far from the question and holding none of its words: never among the 8 a file may give (A6)
    "Lunch is served at noon on Fridays.",
    "The office closes in August.",
    "Parking is behind the building.",
    "Coffee is free in the kitchen.",
]
PROMPT = f"DriveAgent extends Agent {{ … }}\n\n{PROMPT_CACHE_BOUNDARY}\nknowledge_base {{}}"
REPORT = (tt.REPORT_TOOL, {"note": "Read passages of Tom's plan."})


def toward(d: float) -> List[float]:
    """A vector at an angle from the question's (e0) that grows with `d`: smaller `d`, nearer."""
    return [1.0, d, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class World(V2World):
    def __init__(self) -> None:
        super().__init__()
        self.emb.pinned[QUESTION] = toward(0.0)
        rd = self.folder("R&D")
        self.plan = self.file("Tom's plan.docx", parent=rd, kind="docx", chunks=PLAN_TEXTS, capture="digital",
                              content_version="v1", indexed_version="v1")
        for index, d in enumerate([0.1 * (n + 1) for n in range(8)] + [9.0, 9.1, 9.2, 9.3]):
            chunk, _ = self.repo.chunks[("u", self.plan, 1, index)]
            self.repo.chunks[("u", self.plan, 1, index)] = (chunk, toward(d))
        self.other = self.file("menu.md", kind="md", chunks=["Lunch options near the office."])
        self.drive_service = AsyncMock(spec=UserDriveService)
        # the service's own connection reads over the harness connection the index was built for (MULTI_DRIVE_RFC §9)
        reads = UserDriveService(drive=None, connections=harness_connections(self.users, self.drive))
        self.drive_service.status.side_effect = reads.status
        self.drive_service.resolve_ref.side_effect = reads.resolve_ref
        self.operations = DriveOperations(conversion_service=MagicMock(spec=FileConversionService),
                                          drive_service=self.drive_service, drive_search=self.service)

    def agent(self) -> DriveAgent:
        ctx = MagicMock(spec=AgentExecutionContext)
        ctx.provider, ctx.model_name = AsyncMock(), "claude-sonnet-5-5"
        prompts = AsyncMock()
        prompts.build_for_agent.return_value = PROMPT
        agent = DriveAgent(AgentConfig(agent_id="drive_agent_u", agent_type="drive", timeout_ms=DRIVE.timeout_ms),
                           execution_context=ctx, operations=self.operations, prompt_builder=prompts,
                           user_timezone="Europe/Lisbon")
        agent.coordinator = MagicMock()
        return agent


def _msg(context: dict, query: str = QUESTION) -> AgentMessage:
    return AgentMessage.create(sender="coordinator", recipient="drive_agent_u", intent=AgentIntent.QUERY,
                               payload={"query": query, "intent": "work_with_drive_files"},
                               context={"user_id": "u", "account_id": "a", **context})


def _tool_result(request, name: str) -> str:
    """The one result of tool `name` the model read back, from the last request's history."""
    (found,) = [p.tool_response["response"]["result"] for m in request.messages for p in m.parts
                if p.tool_response and p.tool_response.get("name") == name]
    return found


async def test_a_chat_turn_finds_passages_in_a_folder_then_reads_the_neighbours_by_number():
    w = World()
    agent = w.agent()
    agent._call_llm, seen = script(
        calls((tt.FIND_PASSAGES, {"question": QUESTION, "folder": "R&D"})),
        calls((tt.READ_PASSAGES, {"file_ref": f"drive:{w.plan}", "passages": "9-12"})),
        calls(REPORT))
    resp = await agent.execute(_msg({"interactive_delivery": True}))

    assert resp.status == AgentStatus.SUCCESS and len(seen) == 3
    found = _tool_result(seen[-1], tt.FIND_PASSAGES)
    # the specialist's model reads the passages in full, numbered per file from 1; at most 8 from one file
    assert f"[id drive:{w.plan}:1-8 · \"R&D/Tom's plan.docx\" · passages 1–8 of 12]" in found
    assert all(text in found for text in PLAN_TEXTS[:8]) and PLAN_TEXTS[8] not in found
    assert "Lunch options" not in found and "stale:" not in found   # outside the folder; the readable version
    neighbours = _tool_result(seen[-1], tt.READ_PASSAGES)
    assert f"[id drive:{w.plan}:9-12 · \"R&D/Tom's plan.docx\" · passages 9–12 of 12]" in neighbours
    assert all(text in neighbours for text in PLAN_TEXTS[8:])
    # every passage retrieved reaches the orchestrator, and the file enters drive_context once per call, with the
    # passage numbers that call read (§7.4, 2026-10-29: the history line says which passages were read)
    for text in PLAN_TEXTS:
        assert text in resp.result
    entry = {"path": "R&D/Tom's plan.docx", "ref": f"drive:{w.plan}", "op": "passages"}
    assert resp.history_context["drive_context"] == [{**entry, "passages": list(range(1, 9))},
                                                     {**entry, "passages": [9, 10, 11, 12]}]
    w.drive_service.move.assert_not_called()


async def test_a_reminder_run_that_read_passages_has_its_later_move_refused():
    """The planted line in the passage asks for a move; the change in the next step is refused, not run."""
    w = World()
    agent = w.agent()
    agent._call_llm, seen = script(
        calls((tt.FIND_PASSAGES, {"question": QUESTION, "file_refs": f"drive:{w.plan}"})),
        calls((tt.MOVE_FILE, {"file_ref": f"drive:{w.plan}", "folder": "Archive"})),
        calls(REPORT))
    resp = await agent.execute(_msg({"run_origin": "background:reminder"}))

    assert resp.status == AgentStatus.SUCCESS and len(seen) == 3
    assert t.WRITE_AFTER_PASSAGES in _tool_result(seen[-1], tt.MOVE_FILE)
    assert t.WRITE_AFTER_PASSAGES in resp.result
    w.drive_service.move.assert_not_called()
    w.drive_service.get_item.assert_not_called()


async def test_the_same_move_co_emitted_with_the_find_runs_on_a_reminder_run():
    w = World()
    w.drive_service.move.side_effect = RuntimeError("provider down")   # reached: that is what is proved
    agent = w.agent()
    agent._call_llm, seen = script(
        calls((tt.FIND_PASSAGES, {"question": QUESTION}),
              (tt.MOVE_FILE, {"file_ref": f"drive:{w.plan}", "folder": "Archive"})),
        calls(REPORT))
    resp = await agent.execute(_msg({"run_origin": "background:reminder"}))

    assert resp.status == AgentStatus.SUCCESS
    w.drive_service.move.assert_awaited_once()
    assert t.WRITE_AFTER_PASSAGES not in resp.result
