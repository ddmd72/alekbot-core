"""The open bound end to end (USER_DRIVE_INDEXING_RFC §29.6, §29.9): the real `DriveAgent` with a scripted model, its real
tool handlers, `DriveOperations` and the real `DriveSearchService` over the in-memory index. A long indexed annex is
opened: the open is refused before any download, the specialist reads page one, and Smart's answer holds the refusal,
the `pages read` line, the page and the two reading lines, and no block over the cap. Invented universe only."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.drive_agent import DriveAgent
from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_index_params import OPEN_MAX_CHARS
from src.domain.drive_open_texts import ROUTE_SPECIALIST
from src.domain.llm import PROMPT_CACHE_BOUNDARY
from src.domain.user_drive import DriveConnection, DriveItem
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.drive_operations import DriveOperations
from src.ports.llm_port import AgentExecutionContext
from src.services.user_drive_service import UserDriveService
from tests.support.scripted_llm import calls, script
from tests.unit.infrastructure.test_drive_operations_uncovered_paths import FakeConversion
from tests.unit.services.drive_search_v2_fixture import V2World

PATH = "Docs/Oak Street parking rules.md"
CLAUSES = [f"Clause {n}. The tenant keeps the garage gate closed after 23:00." for n in range(1, 131)]
PROMPT = f"DriveAgent extends Agent {{ … }}\n\n{PROMPT_CACHE_BOUNDARY}\nknowledge_base {{}}"
REPORT = (tt.REPORT_TOOL, {"note": "Opened the annex."})
QUESTION = "Give me the first page of the annex Normas de uso del aparcamiento."


class World(V2World):
    def __init__(self) -> None:
        super().__init__()
        docs = self.folder("Docs")
        self.annex = self.file("Oak Street parking rules.md", parent=docs, kind="md", chunks=CLAUSES, capture="digital",
                               content_version="v1", indexed_version="v1")
        self.set(self.annex, text_chars=200_000)
        item = DriveItem(item_id=self.annex, name="Oak Street parking rules.md", path=PATH, is_folder=False,
                         size_bytes=200_000, mime_type="text/markdown")
        self.conversion = FakeConversion({f"drive:{self.annex}": item}, text="\n\n".join(CLAUSES * 40))
        drive = AsyncMock(spec=UserDriveService)
        drive.connection.return_value = DriveConnection(connected=True, drive_id="d1")
        self.operations = DriveOperations(conversion_service=self.conversion, drive_service=drive,
                                          drive_search=self.service)

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


def _msg(context: dict) -> AgentMessage:
    return AgentMessage.create(sender="coordinator", recipient="drive_agent_u", intent=AgentIntent.QUERY,
                               payload={"query": QUESTION, "intent": "work_with_drive_files"},
                               context={"user_id": "u", "account_id": "a", **context})


def _tool_result(request, name: str) -> str:
    (found,) = [p.tool_response["response"]["result"] for m in request.messages for p in m.parts
                if p.tool_response and p.tool_response.get("name") == name]
    return found


@pytest.mark.parametrize("context", [{"interactive_delivery": True}, {"run_origin": "voice:ask_alek"}])
async def test_a_refused_open_is_followed_by_a_page_and_smart_reads_both(context):
    w = World()
    agent = w.agent()
    ref = f"drive:{w.annex}"
    agent._call_llm, seen = script(
        calls((tt.OPEN_DRIVE_FILE, {"file_ref": ref})),
        calls((tt.READ_PASSAGES, {"file_ref": ref, "passages": "from 1"})),
        calls(REPORT))
    resp = await agent.execute(_msg(context))

    assert resp.status == AgentStatus.SUCCESS and len(seen) == 3
    # nothing was downloaded; the specialist's model got the refusal and its own route
    assert w.conversion.text_reads == [] and w.conversion.byte_reads == []
    refused = _tool_result(seen[1], tt.OPEN_DRIVE_FILE)
    assert "never delivered whole" in refused and ROUTE_SPECIALIST.format(ref=ref) in refused
    page = _tool_result(seen[-1], tt.READ_PASSAGES)
    assert "Clause 10." in page and "Clause 11." not in page and "from 11." in page
    # Smart's answer: the refusal block, the one consolidated block with its pages line, the reading lines
    answer = resp.result
    assert "never delivered whole; nothing was downloaded" in answer
    assert "pages read: passages 1–10 of 130" in answer and "next page: from 11" in answer
    reading = [line[len(t.READING_PREFIX):] for line in answer.splitlines() if line.startswith(t.READING_PREFIX)]
    assert t.R_PAGED in reading and t.R_TOO_LONG in reading and t.R_EXCERPTS not in reading
    assert answer.splitlines()[0] == t.STATUS_HEAD.format(status="ok")
    assert len(answer) < OPEN_MAX_CHARS
    assert resp.history_context["drive_context"][0]["op"] == "too_long"
    assert resp.history_context["drive_context"][1]["op"] == "paged"


async def test_a_find_after_a_page_gives_a_mixed_answer():
    w = World()
    agent = w.agent()
    ref = f"drive:{w.annex}"
    w.emb.pinned["gate hours"] = [1.0] + [0.0] * 7
    agent._call_llm, seen = script(
        calls((tt.READ_PASSAGES, {"file_ref": ref, "passages": "from 1"})),
        calls((tt.FIND_PASSAGES, {"question": "gate hours", "file_refs": ref})),
        calls(REPORT))
    resp = await agent.execute(_msg({"interactive_delivery": True}))
    assert resp.status == AgentStatus.SUCCESS
    assert t.R_PAGED in resp.result and t.R_EXCERPTS in resp.result
    assert "pages read: passages 1–10 of 130" in resp.result
