"""A drive connector over production drive services, for the slice-3 access tests (DRIVE_MCP_CONNECTOR_RFC §7, §11
slice 3): `build_drive_connector_tools` + `build_drive_mcp_app` over the production `DriveOperations`,
`DriveReadOperations`, `UserDriveService`, `DriveConnectionService` (in-memory repository) and `DriveSearchService` (the
`MountedWorld` index, its records' ids and parents aligned with the `FakeDriveAdapter` tree: planted first, then
recorded with `item=` / `parent=`). Conversion and notes are fakes with semantics: the conversion answers a planted
file's text, the note store holds the owner's notes and resolves a whole-drive name through the mount table as the
production `DriveNoteService` does. Bearer tokens are verified by a stand-in; texts are markers.

Alex Doe's drive `Drive` (connection `u`, tag `a1`): `R&D/Tom's plan.docx` and `12 Oak Street/FA-0137.pdf`; or, on an
account-wide (Google-like) drive, `Acme Hamburg/Tom's plan.docx` with no reachable parent and `FA-0137.pdf` at the root.
"""
from __future__ import annotations

import itertools
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx

from src.composition.drive_connector_tools import build_drive_connector_tools
from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.domain import drive_connector_contract as c
from src.domain import drive_ref
from src.domain.document_open import OpenedFile
from src.domain.drive_audience import CURRENT_DRIVE_AUDIENCE
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_mounts import unknown_drive_line
from src.domain.drive_note import DriveNoteRefused, NoteRefusal
from src.domain.drive_note_view import NoteView
from src.services.drive_search_service import DriveSearchService
from src.services.mcp_authorization_service import VerifiedAccessToken
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connector_texts import TokenStore, marker
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.support.opened_file import opened_body
from tests.unit.services.drive_mounts_harness import MountedWorld
from tests.unit.services.drive_search_v2_fixture import SPACE

BASE = "https://mcp.example.test"
DRIVE = f"{BASE}/drive-mcp"
AUTH_CONFIG = SimpleNamespace(mcp_resource_uri=f"{BASE}/mcp")
TOKEN = "drive-token-alex"
HEADERS = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}"}
USER = "u"
PLAN_TEXT = "After the first review, Tom sends the plan to Acme Hamburg."
INVOICE_TEXT = "Invoice FA-0137: a lock cylinder and two key copies for 12 Oak Street."
PLAN_NOTE = "Tom's plan for the R&D team at Acme Hamburg."
DRIVE_NOTE = "Alex Doe's work drive."
QUESTION = "What happens after the first review, and what is invoice FA-0137 for?"
NEUTRAL = [marker(c.STATUS).format(status="refused"), marker(c.WHAT_TO_DO).format(line=marker(c.REFUSED_GUIDANCE)),
           marker(c.REFUSED)]


class Auth:
    def verify_access_token(self, token: str, *, resource: Optional[str] = None) -> Optional[VerifiedAccessToken]:
        if token != TOKEN or resource != DRIVE:
            return None
        return VerifiedAccessToken(token=token, client_id="client-1", user_id=USER, account_id="acc-1",
                                   scopes=["drive_read"], resource=DRIVE, expires_at=4_102_444_800)


class Conversion:
    """Answers a planted file's text; records each opened ref."""

    def __init__(self) -> None:
        self.texts: Dict[str, str] = {}
        self.opened: List[str] = []

    async def open(self, ref_: str, user_id: str, account_id: str, **kw: Any) -> OpenedFile:
        self.opened.append(ref_)
        return opened_body(self.texts[ref_])

    async def resolve_bytes(self, ref_: str, user_id: str) -> bytes:
        raise AssertionError("the connector never downloads through resolve_bytes here")


class Notes:
    """The owner's notes by `drive:` ref and by connection id (whole-drive notes). A whole-drive name is resolved through
    the mount table read under the caller's audience, as `DriveNoteService._mount_named` does. Records every read and
    the audience it ran under."""

    def __init__(self, connections: Any) -> None:
        self._connections = connections
        self.by_ref: Dict[str, str] = {}
        self.by_drive: Dict[str, str] = {}
        self.reads: List[str] = []
        self.audiences: List[Any] = []

    async def note_for_item(self, user_id: str, identity: Any) -> None:
        return None

    async def drive_note_texts(self, user_id: str) -> Dict[str, str]:
        return {}

    async def get(self, user_id: str, ref_: str, *, whole_drive_name: str = "") -> Optional[NoteView]:
        self.reads.append(ref_ or whole_drive_name)
        self.audiences.append(CURRENT_DRIVE_AUDIENCE.get())
        if whole_drive_name:
            table = await self._connections.mounts(user_id)
            found = table.by_name(whole_drive_name)
            if found is None:
                raise DriveNoteRefused(NoteRefusal.INVALID_REF, unknown_drive_line(whole_drive_name, table))
            text = self.by_drive.get(found[0].connection_id)
            key = f"drive-name:{found[0].connection_id}"
        else:
            text, key = self.by_ref.get(ref_), ref_
        if text is None:
            return None
        return NoteView(note_id=f"n-{len(self.reads)}", ref=key, note_ref="drive_note:n1", path="", version=1,
                        state="active", text=text)


class World:
    """One user, one drive. `shared=True`: an account-wide drive with `Acme Hamburg` (no reachable parent); `acme=True`:
    a second drive `Acme` (connection `conn-2`, tag `b2`, an empty index), connector access set by `acme_access`."""

    def __init__(self, *, shared: bool = False, acme: bool = False) -> None:
        self.shared = shared
        self.index = MountedWorld(acme=acme)
        self.adapter = (FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT) if shared else FakeDriveAdapter())
        self.handle = self.index.primary.handle
        root = "root"
        if shared:
            self.top = self.adapter.plant_parentless(self.handle, "Acme Hamburg", folder=True)
            self.oak = ""
            self.plan = self.adapter.plant(self.handle, self.top, "Tom's plan.docx", PLAN_TEXT.encode())
            self.invoice = self.adapter.plant(self.handle, root, "FA-0137.pdf", INVOICE_TEXT.encode())
            self.index.folder("Acme Hamburg", parent="", item=self.top)
            self.index.file("FA-0137.pdf", kind="pdf", parent=root, item=self.invoice, chunks=[INVOICE_TEXT])
        else:
            self.top = self.adapter.plant(self.handle, root, "R&D", folder=True)
            self.oak = self.adapter.plant(self.handle, root, "12 Oak Street", folder=True)
            self.plan = self.adapter.plant(self.handle, self.top, "Tom's plan.docx", PLAN_TEXT.encode())
            self.invoice = self.adapter.plant(self.handle, self.oak, "FA-0137.pdf", INVOICE_TEXT.encode())
            self.index.folder("R&D", item=self.top)
            self.index.folder("12 Oak Street", item=self.oak)
            self.index.file("FA-0137.pdf", kind="pdf", parent=self.oak, item=self.invoice, chunks=[INVOICE_TEXT])
        self.index.file("Tom's plan.docx", kind="docx", parent=self.top, item=self.plan, chunks=[PLAN_TEXT])
        counter = itertools.count(1)
        self.index.service = DriveSearchService(self.index.repo, self.index.emb, self.adapter, self.index.connections,
                                                embedding_space=SPACE, clock=lambda: self.index.now,
                                                nonce=lambda: f"nonce{next(counter):04d}")
        self.drive = UserDriveService(self.adapter, self.index.connections)
        self.conversion = Conversion()
        self.conversion.texts = {self.ref(self.plan): PLAN_TEXT, self.ref(self.invoice): INVOICE_TEXT}
        self.notes = Notes(self.index.connections)
        self.notes.by_ref = {self.ref(self.plan): PLAN_NOTE, self.ref(self.invoice): "The locksmith's invoice."}
        self.notes.by_drive = {"u": DRIVE_NOTE}
        self.tokens = TokenStore()
        self.plan_path = "Drive/Acme Hamburg/Tom's plan.docx" if shared else "Drive/R&D/Tom's plan.docx"
        self.invoice_path = "Drive/FA-0137.pdf" if shared else "Drive/12 Oak Street/FA-0137.pdf"

    @staticmethod
    def ref(item_id: str) -> str:
        return drive_ref.format("a1", item_id)

    async def build(self, *, connector: Optional[ConnectorAccess] = None, scope_whole: bool = True,
                    scope_folders: tuple = (), acme_access: Optional[ConnectorAccess] = None) -> "World":
        self.index.primary = replace(self.index.primary, connector_access=connector,
                                     scope_whole_drive=scope_whole and not scope_folders,
                                     scope_folder_ids=scope_folders)
        self.index.acme = replace(self.index.acme, connector_access=acme_access, scope_whole_drive=True)
        await self.index.open()
        self.tools = await build_drive_connector_tools(
            conversion_service=self.conversion, drive_service=self.drive, drive_search=self.index.service,
            drive_notes=self.notes, token_repo=self.tokens, alert_sink=None,
            links=object())  # type: ignore[arg-type]
        return self

    async def connector(self, access: Optional[ConnectorAccess]) -> None:
        await self.index.conns.update_connector_access(USER, "u", access)

    async def call(self, name: str, **arguments: Any) -> str:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        app = build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=self.tools)
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

    async def six(self, item: str, folder: str) -> Dict[str, str]:
        """The six tools aimed at one file (and the folder that holds it): the result text of each."""
        ref = self.ref(item)
        name = "Tom's plan" if item == self.plan else "FA-0137"
        return {
            "search_drive": await self.call("search_drive", query=name),
            "find_passages": await self.call("find_passages", question=QUESTION),
            "read_passages": await self.call("read_passages", file_ref=ref, passages="from 1"),
            "open_drive_file": await self.call("open_drive_file", file_ref=ref),
            "list_folder": await self.call("list_folder", folder=folder),
            "get_note": await self.call("get_note", item_ref=ref),
        }


def what_to_do(text: str) -> str:
    return text.split("\n")[1]
