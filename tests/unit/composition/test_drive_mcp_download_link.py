"""`get_download_link` and the open's media lines end to end on the drive ASGI app (DRIVE_MCP_CONNECTOR_RFC §6.2, §8):
the slice-3 access world (production drive services, connector access on `R&D` + `12 Oak Street`) with a real
`DriveLinkService` over the fake link store, built through `build_drive_link_service`'s parts; the access token
carries `gid`. Texts are markers; `LINK` carries its `{link}`."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx
import pytest

from src.composition.drive_connector_tools import build_drive_connector_tools
from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.domain import drive_connector_contract as c
from src.domain.document_open import OpenedFile
from src.domain.document_read import READER_SKIPPED, ReadOutcome, ReadStatus
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_connector_call import ConnectorCall
from src.domain.drive_link import FOLDER_LINK_LINE
from src.domain.drive_withheld import Withheld
from src.services.drive_link_service import DriveLinkService
from src.services.mcp_authorization_service import VerifiedAccessToken
from tests.support.drive_connector_texts import TokenStore, marker
from tests.support.drive_link_store import FakeDriveLinkStore
from tests.unit.composition.drive_mcp_access_world import AUTH_CONFIG, BASE, DRIVE, HEADERS, NEUTRAL, TOKEN, USER, \
    Conversion, World
from tests.unit.services.drive_link_world import RecordingMedia

GRANT = "grant-alex-000000000000"
LINK_RE = re.compile(r"^<LINK (https://mcp\.example\.test/d/[A-Za-z0-9_-]{22})>$")


class GidAuth:
    def __init__(self, grant: Optional[str]) -> None:
        self.grant = grant

    def verify_access_token(self, token: str, *, resource: Optional[str] = None) -> Optional[VerifiedAccessToken]:
        if token != TOKEN or resource != DRIVE:
            return None
        return VerifiedAccessToken(token=token, client_id="client-1", user_id=USER, account_id="acc-1",
                                   scopes=["drive_read"], resource=DRIVE, expires_at=4_102_444_800,
                                   grant_id=self.grant)


class ScanConversion(Conversion):
    """A scanned PDF answers as `DocumentService` does for the connector: no text, the reader skipped."""

    def __init__(self) -> None:
        super().__init__()
        self.scans: set = set()

    async def open(self, ref_: str, user_id: str, account_id: str, **kw: Any) -> OpenedFile:
        if ref_ in self.scans:
            self.opened.append(ref_)
            return OpenedFile(ReadOutcome(status=ReadStatus.NO_TEXT, detail=f"{READER_SKIPPED}: no_text"),
                              note="[System: no text]")
        return await super().open(ref_, user_id, account_id, **kw)


class LinkWorld(World):
    def __init__(self, grant: Optional[str] = GRANT) -> None:
        super().__init__()
        self.conversion = ScanConversion()
        self.conversion.texts = {self.ref(self.plan): "plan", self.ref(self.invoice): "invoice"}
        self.tokens = TokenStore(LINK="<LINK {link}>")
        self.auth = GidAuth(grant)
        self.store = FakeDriveLinkStore()
        self.media = RecordingMedia()
        self.image = self.adapter.plant(self.handle, self.oak, "12 Oak Street.png", b"png", mime="image/png")
        self.audio = self.adapter.plant(self.handle, self.top, "standup.m4a", b"m4a", mime="audio/mp4")
        self.video = self.adapter.plant(self.handle, self.top, "standup.mp4", b"mp4", mime="video/mp4")
        self.scan = self.adapter.plant(self.handle, self.oak, "F-2026-0311.pdf", b"%PDF scan", mime="application/pdf")
        self.conversion.scans = {self.ref(self.scan)}
        self.links: Any = None

    async def build_links(self, *, connector: Optional[ConnectorAccess] = None) -> "LinkWorld":
        await super().build(connector=connector or ConnectorAccess.folders((self.top, self.oak)))
        if self.links is None:
            self.links = DriveLinkService(self.store, self.drive, self.media, grant_allowed=self._grant,
                                          digest_key=bytes(range(32)), base_url=BASE)
        self.tools = await build_drive_connector_tools(
            conversion_service=self.conversion, drive_service=self.drive, drive_search=self.index.service,
            drive_notes=self.notes, token_repo=self.tokens, alert_sink=None, links=self.links)
        return self

    async def _grant(self, user_id: str, grant_id: Optional[str]) -> bool:
        return grant_id == GRANT

    async def call(self, name: str, **arguments: Any) -> str:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        app = build_drive_mcp_app(authorization_service=self.auth, auth_config=AUTH_CONFIG,  # type: ignore[arg-type]
                                  tools=self.tools)
        transport = httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            async with app.session_manager.run():
                resp = await http.post("/drive-mcp", json=body, headers=HEADERS)
        assert resp.status_code == 200, resp.text
        data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
            if resp.headers["content-type"].startswith("text/event-stream") else resp.text
        res = json.loads(data)["result"]
        assert res["isError"] is False
        return res["content"][0]["text"]


def lines(text: str) -> list:
    return text.split("\n")


@pytest.fixture
async def world():
    return await LinkWorld().build_links()


def test_the_call_value_hides_its_link_from_repr():
    assert "d/" not in repr(ConnectorCall(outcome=None, link=f"{BASE}/d/{'A' * 22}"))  # type: ignore[arg-type]


async def test_get_download_link_answers_the_link_line_and_the_label(world):
    out = lines(await world.call(c.GET_DOWNLOAD_LINK, file_ref=world.ref(world.invoice)))
    assert out[0] == marker(c.STATUS).format(status="ok")
    link = LINK_RE.match(out[1][len("<WHAT_TO_DO "):-1])
    assert link is not None and out[1].startswith("<WHAT_TO_DO <LINK https://mcp.example.test/d/")
    assert out[2].startswith(f"[Drive: {world.invoice_path} (") and out[2].endswith(f"ref={world.ref(world.invoice)}]")
    assert len(out) == 3


async def test_one_link_per_call(world):
    first = await world.call(c.GET_DOWNLOAD_LINK, file_ref=world.ref(world.plan))
    second = await world.call(c.GET_DOWNLOAD_LINK, file_ref=world.ref(world.plan))
    assert len(world.store.links) == 2 and first != second
    assert {link.grant_id for link in world.store.links.values()} == {GRANT}


async def test_an_image_open_carries_one_link_after_its_line(world):
    out = lines(await world.call(c.OPEN_DRIVE_FILE, file_ref=world.ref(world.image)))
    assert out[1] == marker(c.WHAT_TO_DO).format(line=marker(c.text_key_of_withheld(Withheld.IMAGE)))
    assert LINK_RE.match(out[-1]) and sum(line.startswith("<LINK ") for line in out) == 1
    assert len(world.store.links) == 1


@pytest.mark.parametrize("item", ["audio", "video"])
async def test_a_recording_without_transcript_and_a_video_carry_a_link_and_a_scan_does_not(world, item):
    out = lines(await world.call(c.OPEN_DRIVE_FILE, file_ref=world.ref(getattr(world, item))))
    assert out[1] == marker(c.WHAT_TO_DO).format(
        line=marker(c.text_key_of_withheld(Withheld.RECORDING_UNTRANSCRIBED)))
    assert LINK_RE.match(out[-1])
    scan = lines(await world.call(c.OPEN_DRIVE_FILE, file_ref=world.ref(world.scan)))
    assert scan[1] == marker(c.WHAT_TO_DO).format(line=marker(c.text_key_of_withheld(Withheld.SCAN)))
    assert not any(line.startswith("<LINK ") for line in scan)
    assert len(world.store.links) == 1


async def test_a_failed_issue_leaves_the_open_line_without_a_link(world):
    class Broken:
        async def create(self, link: Any) -> None:
            raise RuntimeError("store down")
    world.links._store = Broken()
    out = lines(await world.call(c.OPEN_DRIVE_FILE, file_ref=world.ref(world.image)))
    assert out[1] == marker(c.WHAT_TO_DO).format(line=marker(c.text_key_of_withheld(Withheld.IMAGE)))
    assert not any(line.startswith("<LINK ") for line in out)


async def test_a_token_without_gid_is_refused_neutrally():
    world = await LinkWorld(grant=None).build_links()
    assert lines(await world.call(c.GET_DOWNLOAD_LINK, file_ref=world.ref(world.invoice))) == NEUTRAL
    out = lines(await world.call(c.OPEN_DRIVE_FILE, file_ref=world.ref(world.image)))
    assert not any(line.startswith("<LINK ") for line in out)
    assert world.store.links == {}


async def test_a_file_outside_access_is_the_neutral_refusal():
    world = await LinkWorld().build_links()
    await world.connector(ConnectorAccess.folders((world.top,)))      # 12 Oak Street leaves connector access
    outside = lines(await world.call(c.GET_DOWNLOAD_LINK, file_ref=world.ref(world.invoice)))
    missing = lines(await world.call(c.GET_DOWNLOAD_LINK, file_ref="drive:a1:no-such-item"))
    not_drive = lines(await world.call(c.GET_DOWNLOAD_LINK, file_ref="workspace:notes/plan.md"))
    assert outside == missing == not_drive == NEUTRAL
    assert world.store.links == {}


async def test_a_folder_and_an_empty_ref_are_lines_to_correct(world):
    folder = lines(await world.call(c.GET_DOWNLOAD_LINK, file_ref=world.ref(world.oak)))
    assert folder[0] == marker(c.STATUS).format(status="refused")
    assert folder[1] == marker(c.WHAT_TO_DO).format(line=FOLDER_LINK_LINE.format(path="Drive/12 Oak Street"))
    empty = lines(await world.call(c.GET_DOWNLOAD_LINK, file_ref=""))
    assert empty[0] == marker(c.STATUS).format(status="refused") and len(empty) == 2
    assert world.store.links == {}


async def test_the_log_line_carries_no_ref_and_no_url(world, caplog):
    caplog.set_level(logging.INFO)
    text = await world.call(c.GET_DOWNLOAD_LINK, file_ref=world.ref(world.invoice))
    url = LINK_RE.match(lines(text)[1][len("<WHAT_TO_DO "):-1]).group(1)
    tool_lines = [line for line in caplog.text.splitlines() if "[DriveMCP] tool=get_download_link" in line]
    assert tool_lines and all("status=ok" in line for line in tool_lines)
    assert url not in caplog.text and url.rsplit("/", 1)[1] not in caplog.text
    assert world.ref(world.invoice) not in "\n".join(tool_lines)
