"""The drive connector's read tools end to end on the drive ASGI app (DRIVE_MCP_CONNECTOR_RFC §4, §6, §9, §11 slice 2).

The app is built as `main()` builds it: `build_drive_connector_tools` (the knob on) over the production
`DriveOperations` and `DriveReadOperations`, with a fake drive (one ACTIVE connection tagged `a1`), a fake search
service, a fake conversion service and a fake note store; then `build_drive_mcp_app`. Bearer tokens are verified by a
stand-in for the authorization service (the auth seam has its own tests). Results are read from `tools/call` over
HTTP; texts are markers, never the tokens' wording. Invented items only.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest

from src.composition import drive_connector_tools as dct
from src.composition import drive_mcp_setup
from src.composition.drive_connector_tools import DRIVE_MCP_TOOLS_ENABLED_ENV, build_drive_connector_tools
from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.domain import drive_connector_contract as c
from src.domain.document_open import OpenedFile
from src.domain.document_read import CALLER_CONNECTOR, READER_SKIPPED, ReadOutcome, ReadStatus
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_note_view import NoteView
from src.domain.drive_search import STATUS_OK, DriveSearchCard, DriveSearchResult
from src.domain.drive_text_extent import TextExtent
from src.domain.drive_withheld import Withheld
from src.domain.user_drive import DriveItem, DriveItemNotFoundError
from src.services.mcp_authorization_service import VerifiedAccessToken
from tests.support.drive_connector_texts import TokenStore, marker
from tests.support.drive_service_connections import ConnectionReads, ref
from tests.support.opened_file import opened_body
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, result

BASE = "https://mcp.example.test"
DRIVE = f"{BASE}/drive-mcp"
AUTH_CONFIG = SimpleNamespace(mcp_resource_uri=f"{BASE}/mcp")
TOKEN = "drive-token-u1"
HEADERS = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}"}
QUESTION = "What does Tom's plan say happens after the first review?"

PLAN = DriveItem(item_id="d1", name="Tom's plan.docx", path="R&D/Tom's plan.docx", is_folder=False, size_bytes=1000,
                 mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
VIDEO = DriveItem(item_id="v1", name="standup.mp4", path="R&D/standup.mp4", is_folder=False, size_bytes=4000,
                  mime_type="video/mp4")
AUDIO = DriveItem(item_id="r1", name="standup.m4a", path="R&D/standup.m4a", is_folder=False, size_bytes=2000,
                  mime_type="audio/mp4")
PHOTO = DriveItem(item_id="i1", name="12 Oak Street.png", path="12 Oak Street/12 Oak Street.png", is_folder=False,
                  size_bytes=2000, mime_type="image/png")
SCAN = DriveItem(item_id="p1", name="FA-0137.pdf", path="12 Oak Street/FA-0137.pdf", is_folder=False,
                 size_bytes=3000, mime_type="application/pdf")
WEBM = DriveItem(item_id="w1", name="standup.webm", path="R&D/standup.webm", is_folder=False, size_bytes=4000,
                 mime_type="video/webm")
ARCHIVED = DriveItem(item_id="t1", name="standup.txt", path="_Transcripts/standup.txt", is_folder=False,
                     size_bytes=500, mime_type="text/plain")
ITEMS = {i.item_id: i for i in (PLAN, VIDEO, AUDIO, PHOTO, SCAN, WEBM, ARCHIVED)}
PLAN_TEXT = "After the first review, Tom sends invoice FA-0137."


class Auth:
    """The authorization service's bearer check: one drive token for user u1 (account acc1)."""

    def verify_access_token(self, token: str, *, resource: Optional[str] = None) -> Optional[VerifiedAccessToken]:
        if token != TOKEN or resource != DRIVE:
            return None
        return VerifiedAccessToken(token=token, client_id="client-1", user_id="u1", account_id="acc1",
                                   scopes=["drive_read"], resource=DRIVE, expires_at=4_102_444_800)


class Conversion:
    """Records every download and every open's caller; the scan answers as `DocumentService` does for the connector
    (no reader run)."""

    def __init__(self) -> None:
        self.downloads: List[str] = []
        self.callers: List[str] = []
        self.reader_runs = 0

    async def resolve_bytes(self, ref_: str, user_id: str) -> bytes:
        self.downloads.append(ref_)
        return b"frames"

    async def open(self, ref_: str, user_id: str, account_id: str, **kw: Any) -> OpenedFile:
        self.callers.append(kw["caller"])
        if ref_ == ref(SCAN.item_id):
            if kw["caller"] != CALLER_CONNECTOR:
                self.reader_runs += 1
            return OpenedFile(ReadOutcome(status=ReadStatus.NO_TEXT, detail=f"{READER_SKIPPED}: no_text"),
                              note="[System: no text]")
        return opened_body(PLAN_TEXT)


class Drive(ConnectionReads):
    async def get_item(self, handle: Any, item_id: str) -> DriveItem:
        if item_id not in ITEMS:
            raise DriveItemNotFoundError(item_id)
        return ITEMS[item_id]

    async def list_folder_by_id(self, handle: Any, item_id: str):
        raise DriveItemNotFoundError(item_id)   # only a missing folder is listed by ref here

    async def scope_folder_paths(self, handle: Any):
        return [], 0

    async def write_folder_path(self, handle: Any) -> str:
        return "R&D"


class Search:
    """The search service's reads: one card for a search, one passage for a find, a first page for a range read."""

    def __init__(self) -> None:
        self.queries: List[str] = []

    async def search(self, user_id: str, request: Any, **kw: Any) -> DriveSearchResult:
        self.queries.append(request.query)
        card = DriveSearchCard(ref=ref(PLAN.item_id), item_id=PLAN.item_id, name=PLAN.name, path=PLAN.path,
                               kind="docx", is_folder=False, summary=f"{PLAN.path} | Word document",
                               nonce="nonce0001")
        return DriveSearchResult(status=STATUS_OK, cards=(card,), ref_tag="a1")

    async def excerpts(self, user_id: str, request: Any, **kw: Any):
        self.queries.append(request.question)
        file = excerpt_file(PLAN.item_id, PLAN.path, [chunk(PLAN.item_id, 0, PLAN_TEXT)], ref_tag="a1")
        return result(file, question=request.question, ref_tag="a1")

    async def excerpt_range(self, user_id: str, request: Any):
        span = PageSpan(first=1, last=10, total=30, version=2, next=11, path=PLAN.path, ref=ref(PLAN.item_id))
        file = excerpt_file(PLAN.item_id, PLAN.path, [chunk(PLAN.item_id, i, f"Step {i + 1}.") for i in (0, 1)],
                            ref_tag="a1")
        return result(file, request=ExcerptRangeRequest(ref(PLAN.item_id), 1, 10, page=True), page=span,
                      ref_tag="a1")

    async def access_note(self, conn: Any, **kw: Any) -> Optional[str]:
        return None   # the index knows no item the bot cannot open

    async def text_extent(self, conn: Any, item_id: str) -> TextExtent:
        return TextExtent.UNKNOWN

    async def archived_text(self, conn: Any, item_id: str, **kw: Any) -> Optional[str]:
        """A file of the bot's transcripts folder with no pinned entry: its downloaded text, read through `file_text`."""
        if item_id != ARCHIVED.item_id:
            return None
        return await kw["file_text"]()

    async def transcript_text(self, conn: Any, item_id: str, **kw: Any) -> Optional[str]:
        return None


class Notes:
    """The note store's reads: the owner's note on Tom's plan only; `fail` makes `get` raise it."""

    def __init__(self) -> None:
        self.reads: List[str] = []
        self.fail: Optional[Exception] = None

    async def note_for_item(self, user_id: str, identity: Any) -> None:
        return None

    async def drive_note_texts(self, user_id: str) -> Dict[str, str]:
        return {}

    async def get(self, user_id: str, ref_: str, *, whole_drive_name: str = "") -> Optional[NoteView]:
        self.reads.append(ref_ or whole_drive_name)
        if self.fail is not None:
            raise self.fail
        if ref_ != ref(PLAN.item_id):
            return None
        return NoteView(note_id="n1", ref=ref_, note_ref="drive_note:n1", path=PLAN.path, version=2, state="active",
                        text="Tom's plan for the R&D team at Acme Hamburg.")


class Sink:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


class World:
    def __init__(self, tokens: Optional[TokenStore] = None) -> None:
        self.conversion, self.drive, self.search, self.notes = Conversion(), Drive(), Search(), Notes()
        self.tokens = tokens if tokens is not None else TokenStore()
        self.sink = Sink()

    async def build(self):
        self.tools = await build_drive_connector_tools(
            conversion_service=self.conversion, drive_service=self.drive, drive_search=self.search,
            drive_notes=self.notes, token_repo=self.tokens, alert_sink=self.sink)
        self.app = build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=self.tools)
        return self

    async def call(self, name: str, **arguments: Any) -> Dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        return await self._post(body)

    async def list_tools(self) -> List[Dict[str, Any]]:
        return (await self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))["tools"]

    def fresh_app(self):
        """A new app over the same tools: an app's session manager starts once per instance."""
        return build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=self.tools)

    async def post(self, body: Dict[str, Any], headers: Dict[str, str]) -> httpx.Response:
        app = self.fresh_app()
        transport = httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            async with app.session_manager.run():
                return await http.post("/drive-mcp", json=body, headers=headers)

    async def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self.post(body, HEADERS)
        assert resp.status_code == 200, resp.text
        data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
            if resp.headers["content-type"].startswith("text/event-stream") else resp.text
        return json.loads(data)["result"]


def text_of(res: Dict[str, Any]) -> str:
    assert res["isError"] is False
    assert [block["type"] for block in res["content"]] == ["text"]
    assert "structuredContent" not in res or res["structuredContent"] is None
    return res["content"][0]["text"]


NEUTRAL = [marker(c.STATUS).format(status="refused"), marker(c.WHAT_TO_DO).format(line=marker(c.REFUSED_GUIDANCE)),
           marker(c.REFUSED)]


def what_to_do(text: str) -> str:
    return text.split("\n")[1]


@pytest.fixture
async def world(monkeypatch):
    monkeypatch.setenv(DRIVE_MCP_TOOLS_ENABLED_ENV, "on")
    return await World().build()


class TestKnob:
    async def test_knob_off_lists_no_tool(self, monkeypatch):
        monkeypatch.delenv(DRIVE_MCP_TOOLS_ENABLED_ENV, raising=False)
        built = await World().build()
        assert built.tools is None
        assert await built.list_tools() == []

    async def test_knob_on_lists_the_six_tools_each_read_only(self, world):
        tools = await world.list_tools()
        assert [t["name"] for t in tools] == list(c.TOOL_NAMES)
        assert all(t["annotations"]["readOnlyHint"] is True for t in tools)


class TestHappyPaths:
    async def test_the_six_read_tools_are_listed_and_nothing_else(self, world):
        assert [t["name"] for t in await world.list_tools()] == list(c.TOOL_NAMES)

    async def test_search_drive_answers_from_its_cards(self, world):
        text = text_of(await world.call("search_drive", query="Tom's plan"))
        assert text.startswith(marker(c.STATUS).format(status="ok"))
        assert what_to_do(text) == marker(c.WHAT_TO_DO).format(line=marker(c.ANSWER_FROM_RESULTS))
        assert "R&D/Tom's plan.docx" in text and "Reading: <R_FIND>" in text
        assert world.search.queries == ["Tom's plan"]

    async def test_find_passages_shows_the_passage_and_the_language_line(self, world):
        text = text_of(await world.call("find_passages", question=QUESTION, file_refs=[ref("d1")]))
        assert PLAN_TEXT in text and "Reading: <R_EXCERPTS>" in text
        assert text.endswith(marker(c.LANGUAGE))

    async def test_read_passages_prints_the_connector_next_page_line(self, world):
        text = text_of(await world.call("read_passages", file_ref=ref("d1"), passages="from 1"))
        assert "<PAGE_NEXT 1-10 of 30" in text and "next 11>" in text
        assert "when the commission asks" not in text

    async def test_open_drive_file_returns_the_text_with_the_open_lines(self, world):
        text = text_of(await world.call("open_drive_file", file_ref=ref("d1")))
        assert PLAN_TEXT in text and "Reading: <R_NOT_READ>" in text
        assert world.conversion.callers == [CALLER_CONNECTOR]

    async def test_list_folder_root_lists_the_drives(self, world):
        text = text_of(await world.call("list_folder", folder="/"))
        assert what_to_do(text) == marker(c.WHAT_TO_DO).format(line=marker(c.ANSWER_FROM_RESULTS))
        assert len(text.split("\n")) > 2

    async def test_get_note_reads_the_owner_note_of_a_drive_ref(self, world):
        text = text_of(await world.call("get_note", item_ref=ref("d1")))
        assert "Tom's plan for the R&D team at Acme Hamburg." in text and "Reading: <R_OWNER_NOTE>" in text


class TestOpenWithholds:
    # every video is a recording to the kind classifier: one with no transcript is "not transcribed", never a post
    @pytest.mark.parametrize("item, code", [(VIDEO, Withheld.RECORDING_UNTRANSCRIBED), (PHOTO, Withheld.IMAGE),
                                            (AUDIO, Withheld.RECORDING_UNTRANSCRIBED),
                                            (WEBM, Withheld.RECORDING_UNTRANSCRIBED), (SCAN, Withheld.SCAN)])
    async def test_media_answer_their_line_without_a_download_or_a_post(self, world, item, code):
        text = text_of(await world.call("open_drive_file", file_ref=ref(item.item_id)))
        assert what_to_do(text) == marker(c.WHAT_TO_DO).format(line=marker(c.withheld_key(code)))
        assert world.conversion.downloads == []      # a video is posted only after its download
        assert world.conversion.reader_runs == 0

    async def test_a_workspace_ref_is_the_neutral_refusal_before_any_call(self, world):
        text = text_of(await world.call("open_drive_file", file_ref="workspace:notes/plan.md"))
        assert text.split("\n") == NEUTRAL
        assert world.conversion.callers == []

    async def test_a_ref_that_does_not_exist_answers_the_same_neutral_refusal(self, world):
        missing = text_of(await world.call("open_drive_file", file_ref=ref("x9")))
        outside = text_of(await world.call("open_drive_file", file_ref="workspace:notes/plan.md"))
        listed = text_of(await world.call("list_folder", folder=ref("x9")))
        assert missing == outside == listed
        assert missing.split("\n") == NEUTRAL

    async def test_a_transcripts_folder_file_is_read_as_the_connector(self, world):
        text = text_of(await world.call("open_drive_file", file_ref=ref(ARCHIVED.item_id)))
        assert PLAN_TEXT in text
        assert world.conversion.callers == [CALLER_CONNECTOR]

    async def test_an_empty_ref_is_a_line_to_correct(self, world):
        text = text_of(await world.call("open_drive_file", file_ref=""))
        assert text.split("\n")[0] == marker(c.STATUS).format(status="refused")
        assert "file_ref is required" in what_to_do(text)

    async def test_the_connector_operations_have_no_notifier_and_no_localization(self, monkeypatch):
        seen: Dict[str, Any] = {}
        real = dct.DriveOperations

        def spy(**kwargs: Any):
            seen.update(kwargs)
            return real(**kwargs)

        monkeypatch.setenv(DRIVE_MCP_TOOLS_ENABLED_ENV, "1")
        monkeypatch.setattr(dct, "DriveOperations", spy)
        await World().build()
        assert seen["notification"] is None and seen["localization"] is None and seen["language_service"] is None


class TestGetNote:
    async def test_a_drive_note_ref_is_refused_without_a_read(self, world):
        text = text_of(await world.call("get_note", item_ref="drive_note:n1"))
        assert text.split("\n")[1:] == [marker(c.WHAT_TO_DO).format(line=marker(c.REFUSED_GUIDANCE)),
                                        marker(c.REFUSED)]
        assert world.notes.reads == []

    async def test_no_target_or_two_targets_is_a_line_to_correct(self, world):
        none = text_of(await world.call("get_note"))
        both = text_of(await world.call("get_note", item_ref=ref("d1"), whole_drive_name="Acme"))
        assert none == both and "Give exactly one target" in what_to_do(none)
        assert world.notes.reads == []

    async def test_a_file_with_no_note_answers_the_neutral_refusal(self, world):
        text = text_of(await world.call("get_note", item_ref=ref("v1")))
        assert text.split("\n") == NEUTRAL

    async def test_a_store_error_carrying_note_text_is_logged_by_type_only(self, world, caplog):
        world.notes.fail = ValueError("noted by the owner: Tom's private plan for Acme Hamburg")
        with caplog.at_level(logging.DEBUG):
            text = text_of(await world.call("get_note", item_ref=ref("d1")))
        assert text.split("\n")[0] == marker(c.STATUS).format(status="failed")
        logged = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
        assert "private plan" not in logged and "ValueError" in logged
        assert "private plan" not in text


class TestInputIsLiberal:
    async def test_a_number_for_passages_is_accepted(self, world):
        text = text_of(await world.call("read_passages", file_ref=ref("d1"), passages=3))
        assert text.split("\n")[0] == marker(c.STATUS).format(status="ok")

    async def test_null_optional_fields_are_empty(self, world):
        text = text_of(await world.call("search_drive", query="Tom's plan", folder=None, drives=None, kind=None))
        assert text.split("\n")[0] == marker(c.STATUS).format(status="ok")


class TestObservability:
    async def test_one_log_line_per_call_without_the_user_words(self, world, caplog):
        with caplog.at_level(logging.INFO):
            text_of(await world.call("find_passages", question=QUESTION, folder="R&D"))
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveMCP] tool=")]
        assert len(lines) == 1
        assert lines[0].startswith("[DriveMCP] tool=find_passages user=u1 status=ok chars=")
        assert " ms=" in lines[0]
        everything = "\n".join(r.getMessage() for r in caplog.records)
        assert QUESTION not in everything and "first review" not in everything


class TestSizeGuard:
    async def test_a_result_over_the_bound_is_cut_with_the_over_size_line(self, world, monkeypatch):
        monkeypatch.setattr(drive_mcp_setup, "RESULT_MAX_CHARS", 200)   # no fake read reaches the real 100,000
        text = text_of(await world.call("open_drive_file", file_ref=ref("d1")))
        assert len(text) <= 200 and text.endswith(marker(c.OVER_SIZE))
        assert text.startswith(marker(c.STATUS).format(status="ok"))


class TestTimeout:
    async def test_a_call_past_the_budget_answers_unfinished(self, world, monkeypatch):
        async def slow(*a: Any, **kw: Any):
            await asyncio.sleep(1)

        monkeypatch.setattr(drive_mcp_setup, "CALL_TIMEOUT_S", 0.01)
        monkeypatch.setattr(world.search, "search", slow)
        text = text_of(await world.call("search_drive", query="Tom's plan"))
        assert text.split("\n") == [marker(c.STATUS).format(status="unfinished"),
                                    marker(c.WHAT_TO_DO).format(line=marker(c.UNFINISHED))]


class TestAuth:
    async def test_no_token_is_401_before_any_tool(self, world):
        resp = await world.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"Accept": HEADERS["Accept"]})
        assert resp.status_code == 401
        assert world.search.queries == []
