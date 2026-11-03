"""The drive connector's deploy knob and boot rule (DRIVE_MCP_CONNECTOR_RFC §5.3, §11 slice 2): off, the connector
loads nothing and lists no tool while its auth seam still answers; on, the texts load once at build, and a failed first
load raises (with one ops alert) so the drive app is never built."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, List, Optional

import httpx
import pytest

from src.composition.drive_connector_tools import build_drive_connector_tools, build_drive_connector_tools_from_container
from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.domain import drive_connector_contract as c
from src.domain.exceptions import ConnectorTextsUnavailable
from src.services.mcp_authorization_service import VerifiedAccessToken
from tests.support.drive_connector_texts import RESULT_TOKEN, TokenStore, marker

BASE = "https://mcp.example.test"
DRIVE = f"{BASE}/drive-mcp"
AUTH_CONFIG = SimpleNamespace(mcp_resource_uri=f"{BASE}/mcp")
TOKEN = "drive-token-u1"
HEADERS = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}"}


class Auth:
    def verify_access_token(self, token: str, *, resource: Optional[str] = None) -> Optional[VerifiedAccessToken]:
        if token != TOKEN or resource != DRIVE:
            return None
        return VerifiedAccessToken(token=token, client_id="client-1", user_id="u1", account_id="acc1",
                                   scopes=["drive_read"], resource=DRIVE, expires_at=4_102_444_800)


class CountingStore(TokenStore):
    def __init__(self, **overrides: str) -> None:
        super().__init__(**overrides)
        self.reads: List[str] = []

    async def get(self, token_id):
        self.reads.append(str(token_id))
        return await super().get(token_id)


class Sink:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


async def build(store: Any, sink: Optional[Sink] = None, *, conversion: Any = "conversion"):
    return await build_drive_connector_tools(conversion_service=conversion, drive_service=None, drive_search=None,
                                             drive_notes=None, token_repo=store, alert_sink=sink,
                                             links=object())  # type: ignore[arg-type]


async def rpc(app, method: str, params: Optional[dict] = None, headers=HEADERS) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}
    transport = httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        async with app.session_manager.run():
            return await http.post("/drive-mcp", json=body, headers=headers)


def result_of(resp: httpx.Response) -> dict:
    data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
        if resp.headers["content-type"].startswith("text/event-stream") else resp.text
    return json.loads(data)["result"]


class TestOff:
    async def test_off_the_app_lists_no_tool_has_no_instructions_and_still_refuses_anonymous_calls(self):
        app = build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=None)
        assert result_of(await rpc(app, "tools/list"))["tools"] == []
        app = build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=None)
        init = result_of(await rpc(app, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                       "clientInfo": {"name": "probe", "version": "1"}}))
        assert init.get("instructions") is None
        app = build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=None)
        assert (await rpc(app, "tools/list", headers={"Accept": HEADERS["Accept"]})).status_code == 401

    async def test_off_a_tool_call_is_an_error(self):
        app = build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=None)
        res = result_of(await rpc(app, "tools/call", {"name": "search_drive", "arguments": {"query": "FA-0137"}}))
        assert res["isError"] is True


class TestOn:
    async def test_on_loads_the_texts_once_and_lists_the_tools(self, monkeypatch):
        store = CountingStore()
        tools = await build(store)
        assert tools is not None and tools.notes is None
        assert tools.initial_texts.tools[c.INSTRUCTIONS_KEY] == marker(c.INSTRUCTIONS_KEY)
        assert sorted(store.reads) == sorted(["MCP_DRIVE_TOOL_TEXTS", "MCP_DRIVE_RESULT_TEXTS"])
        app = build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=tools)
        names = [t["name"] for t in result_of(await rpc(app, "tools/list"))["tools"]]
        assert names == [n for n in c.TOOL_NAMES if n != "get_note"]   # no note service: no get_note

    async def test_a_missing_token_raises_and_alerts_once(self, monkeypatch):
        store, sink = TokenStore(), Sink()
        del store.tokens[RESULT_TOKEN]
        with pytest.raises(ConnectorTextsUnavailable):
            await build(store, sink)
        assert len(sink.posts) == 1 and RESULT_TOKEN in sink.posts[0]

    async def test_a_token_missing_a_key_raises(self, monkeypatch):
        store = TokenStore()
        tool = store.tokens["MCP_DRIVE_TOOL_TEXTS"]
        store.tokens["MCP_DRIVE_TOOL_TEXTS"] = type(tool)(
            id=tool.id, category=tool.category, class_=tool.class_, metadata=tool.metadata,
            content=tool.content.replace("### get_note.item_ref", "### get_note.ref"))
        with pytest.raises(ConnectorTextsUnavailable):
            await build(store)

    @pytest.mark.parametrize("missing", ["conversion", "token_repo"])
    async def test_on_without_conversion_or_the_token_store_raises(self, monkeypatch, missing):
        with pytest.raises(RuntimeError):
            if missing == "conversion":
                await build(TokenStore(), conversion=None)
            else:
                await build(None)


class BrokenSink:
    async def post(self, text: str) -> None:
        raise ConnectionError("webhook down")


async def test_an_alert_that_cannot_be_delivered_never_hides_the_failed_load(monkeypatch, caplog):
    store = TokenStore()
    del store.tokens[RESULT_TOKEN]
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectorTextsUnavailable):
        await build(store, BrokenSink())  # type: ignore[arg-type]
    assert any("alert not delivered (ConnectionError)" in r.getMessage() for r in caplog.records)


def container(store: Any, *, notes: Any = None, assembly: bool = True) -> SimpleNamespace:
    return SimpleNamespace(file_conversion_service="conversion", user_drive_service=None, drive_search_service=None,
                           drive_note_service=notes,
                           assembly_service=SimpleNamespace(token_repo=store) if assembly else None)


class TestFromContainer:
    async def test_the_container_singletons_reach_the_tools(self, monkeypatch):
        notes = object()
        tools = await build_drive_connector_tools_from_container(container(TokenStore(), notes=notes),
                                                                 alert_sink=None)  # type: ignore[arg-type]
        assert tools is not None and tools.notes is notes
        assert tools.initial_texts.tools[c.INSTRUCTIONS_KEY] == marker(c.INSTRUCTIONS_KEY)

    async def test_no_assembly_service_means_no_token_store(self, monkeypatch):
        with pytest.raises(RuntimeError, match="token store"):
            await build_drive_connector_tools_from_container(container(TokenStore(), assembly=False),
                                                             alert_sink=None)  # type: ignore[arg-type]
