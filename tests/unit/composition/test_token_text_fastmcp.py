"""TokenTextFastMCP on the drive app (DRIVE_MCP_CONNECTOR_RFC §5.3, §5.4, §6.1, E9, E15, E17): every tool and parameter
text and the server instructions come from the tokens at request time; a token upload shows within the cache TTL
without a restart; the SDK-built schema keeps its types and required list; every tool is read-only; only the open
declares its result size. Marker texts only."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any, Dict, Optional

import httpx
import pytest

from src.composition.drive_connector_tools import DriveConnectorTools
from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.composition.token_text_fastmcp import TokenTextFastMCP
from src.domain import drive_connector_contract as c
from src.domain.exceptions import ConnectorTextsUnavailable
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_read_operations import DriveReadOperations
from src.services.connector_text_service import ConnectorTextService
from src.services.mcp_authorization_service import VerifiedAccessToken
from tests.support.drive_connector_texts import RESULT_TOKEN, TOOL_TOKEN, TokenStore, marker

BASE = "https://mcp.example.test"
DRIVE = f"{BASE}/drive-mcp"
AUTH_CONFIG = SimpleNamespace(mcp_resource_uri=f"{BASE}/mcp")
TOKEN = "drive-token-u1"
HEADERS = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}"}
REQUIRED = {"search_drive": [], "find_passages": ["question"], "read_passages": ["file_ref", "passages"],
            "open_drive_file": ["file_ref"], "list_folder": [], "get_note": []}


class Auth:
    def verify_access_token(self, token: str, *, resource: Optional[str] = None) -> Optional[VerifiedAccessToken]:
        if token != TOKEN or resource != DRIVE:
            return None
        return VerifiedAccessToken(token=token, client_id="client-1", user_id="u1", account_id="acc1",
                                   scopes=["drive_read"], resource=DRIVE, expires_at=4_102_444_800)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self) -> None:
        self.store = TokenStore()
        self.clock = Clock()
        self.texts = ConnectorTextService(self.store, tool_token_id=TOOL_TOKEN, result_token_id=RESULT_TOKEN,
                                          required_tool_keys=c.REQUIRED_TOOL_KEYS,
                                          required_result_keys=c.REQUIRED_RESULT_KEYS, ttl_s=60.0, clock=self.clock)

    async def build(self, texts: Any = None):
        initial = await self.texts.texts()
        tools = DriveConnectorTools(texts=texts or self.texts, initial_texts=initial,
                                    operations=DriveReadOperations(DriveOperations(conversion_service=None)),  # type: ignore[arg-type]
                                    notes=object())  # type: ignore[arg-type]
        self.app = build_drive_mcp_app(authorization_service=Auth(), auth_config=AUTH_CONFIG, tools=tools)
        return self

    async def listed(self) -> Dict[str, Any]:
        return {tool.name: tool for tool in await self.app.list_tools()}


@pytest.fixture
async def world():
    return await World().build()


async def test_every_description_comes_from_the_tool_texts(world):
    listed = await world.listed()
    assert list(listed) == list(c.TOOL_NAMES)
    for name, tool in listed.items():
        assert tool.description == marker(name)
        props = tool.inputSchema["properties"]
        assert list(props) == list(c.TOOL_PARAMETERS[name])
        assert {p: v["description"] for p, v in props.items()} == {p: marker(c.text_key_of_parameter(name, p)) for p in props}


async def test_an_upload_shows_after_the_ttl_without_a_restart(world):
    world.store.upload(**{"search_drive": "<search v2>", "find_passages.question": "<question v2>"})
    world.clock.now += 30
    assert (await world.listed())["search_drive"].description == marker("search_drive")
    world.clock.now += 31
    listed = await world.listed()
    assert listed["search_drive"].description == "<search v2>"
    assert listed["find_passages"].inputSchema["properties"]["question"]["description"] == "<question v2>"


async def test_the_schema_keeps_its_types_and_required_and_the_registered_tools_are_untouched(world):
    registered = {info.name: copy.deepcopy(info.parameters) for info in world.app._tool_manager.list_tools()}
    listed = await world.listed()
    for name, tool in listed.items():
        schema = copy.deepcopy(tool.inputSchema)
        for prop in schema["properties"].values():
            del prop["description"]
        assert schema == registered[name]
        assert sorted(schema.get("required", [])) == sorted(REQUIRED[name])
        assert all(prop["type"] == "string" for prop in schema["properties"].values())
    assert {info.name: info.parameters for info in world.app._tool_manager.list_tools()} == registered


async def test_every_tool_is_read_only_and_only_the_open_declares_its_result_size(world):
    listed = await world.listed()
    assert all(tool.annotations.readOnlyHint is True for tool in listed.values())
    metas = {name: tool.meta for name, tool in listed.items()}
    assert metas.pop("open_drive_file") == {"anthropic/maxResultSizeChars": 100_000}
    assert set(metas.values()) == {None}


async def test_the_instructions_come_from_the_tokens_and_follow_an_upload(world):
    options = world.app._mcp_server.create_initialization_options()
    assert options.instructions == marker(c.INSTRUCTIONS_KEY)
    assert options.capabilities.tools.listChanged is False
    world.store.upload(instructions="<instructions v2>")
    world.clock.now += 61
    await world.listed()
    assert world.app._mcp_server.create_initialization_options().instructions == "<instructions v2>"


async def test_initialize_over_http_carries_the_token_instructions(world):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "probe", "version": "1"}}}
    transport = httpx.ASGITransport(app=world.app.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        async with world.app.session_manager.run():
            resp = await http.post("/drive-mcp", json=body, headers=HEADERS)
    data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
        if resp.headers["content-type"].startswith("text/event-stream") else resp.text
    result = json.loads(data)["result"]
    assert result["instructions"] == marker(c.INSTRUCTIONS_KEY)
    assert result["serverInfo"]["name"] == "alekbot-drive"


class NeverLoaded:
    async def texts(self):
        raise ConnectorTextsUnavailable("connector texts could not be loaded: token missing")


async def test_a_call_with_no_texts_is_an_error_result():
    world = await World().build(texts=NeverLoaded())
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_folder", "arguments": {"folder": "/"}}}
    # tools/call refreshes the low-level tool cache through tools/list, which needs the texts too: the client gets
    # an error either way, never a result without its texts.
    transport = httpx.ASGITransport(app=world.app.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        async with world.app.session_manager.run():
            resp = await http.post("/drive-mcp", json=body, headers=HEADERS)
    data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
        if resp.headers["content-type"].startswith("text/event-stream") else resp.text
    message = json.loads(data)
    assert message["result"]["isError"] is True


async def test_a_bare_token_text_app_fills_its_one_tool_from_the_texts():
    world = World()
    app = TokenTextFastMCP(texts=world.texts, initial=await world.texts.texts(), name="probe")

    @app.tool(name="list_folder", meta={"probe": 1})
    async def list_folder(folder: str = "") -> str:
        return folder

    [tool] = await app.list_tools()
    assert (tool.name, tool.description) == ("list_folder", marker("list_folder"))
    assert tool.inputSchema["properties"]["folder"]["description"] == marker("list_folder.folder")
    assert tool.annotations.readOnlyHint is True and tool.meta == {"probe": 1}
    assert app.instructions is None   # FastMCP's own instructions stay empty: the texts serve them
    assert app._mcp_server.create_initialization_options().instructions == marker(c.INSTRUCTIONS_KEY)


async def test_the_drive_app_with_tools_is_a_token_text_app(world):
    assert isinstance(world.app, TokenTextFastMCP)


class LoadedOnce:
    """Texts that load at boot, then become unavailable (a defensive branch: the real service keeps the last good)."""

    def __init__(self, texts: Any) -> None:
        self._texts, self.calls = texts, 0

    async def texts(self):
        self.calls += 1
        if self.calls > 1:
            raise ConnectorTextsUnavailable("connector texts could not be loaded: token missing")
        return self._texts


async def test_a_call_after_the_texts_became_unavailable_is_an_error_result():
    world = World()
    world = await world.build(texts=LoadedOnce(await world.texts.texts()))
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_folder", "arguments": {}}}
    transport = httpx.ASGITransport(app=world.app.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        async with world.app.session_manager.run():
            await http.post("/drive-mcp", json=listing, headers=HEADERS)   # fills the server's tool cache
            resp = await http.post("/drive-mcp", json=call, headers=HEADERS)
    data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
        if resp.headers["content-type"].startswith("text/event-stream") else resp.text
    result = json.loads(data)["result"]
    assert result["isError"] is True and "not available" in result["content"][0]["text"]
