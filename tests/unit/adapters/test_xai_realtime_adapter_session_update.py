"""A mid-call session update on xAI, at the websocket boundary (the interpreter's language pair,
decisions/lelik_voice_modes.md): one `session.update` with only what changes; a tools update keeps the session's
built-ins and MCP entries from open() beside the new function tools."""
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.remote_tool_server import RemoteToolServer
from tests.contracts.adapter_contracts import XAI_REALTIME_MID_CALL_SESSION_UPDATE

_TOKEN = "token-placeholder-0001"
_SERVER = RemoteToolServer("server_one", "https://mcp.example.test/mcp", "A placeholder server.", ("tool_a",),
                           token=_TOKEN)
_PAIR_TOOL = {"name": "set_language_pair", "description": "d", "parameters": {"type": "object"}}
_OTHER_TOOL = {"name": "tool_b", "description": "d", "parameters": {"type": "object"}}
_BUILTINS = [{"type": "web_search"}, {"type": "x_search"}]


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        pass


async def _opened(*, builtin_search: bool = False, servers=(), tools=()):
    ws = FakeWebSocket()
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws),
                                 builtin_search=builtin_search)
    await adapter.open(instructions="base", reasoning_effort="high", tools=list(tools), mcp_servers=servers)
    ws.sent.clear()
    return adapter, ws


def _entry(server: RemoteToolServer) -> dict:
    return {"type": "mcp", "server_url": server.url, "server_label": server.label,
            "server_description": server.description, "allowed_tools": list(server.allowed_tools),
            "authorization": server.token}


def _validate(update: dict, functions=(), entries=()) -> None:
    XAI_REALTIME_MID_CALL_SESSION_UPDATE.validate("xai_realtime", {
        **update, "expected_function_names": list(functions), "expected_mcp_entries": list(entries)})


async def test_an_instructions_update_sends_only_the_instructions():
    adapter, ws = await _opened(builtin_search=True, servers=(_SERVER,))

    await adapter.update_session(instructions="base\n\nblock")

    assert ws.sent == [{"type": "session.update", "session": {"instructions": "base\n\nblock"}}]
    _validate(ws.sent[0])


async def test_the_cache_boundary_is_stripped_from_new_instructions():
    adapter, ws = await _opened()

    await adapter.update_session(instructions="static\n<!-- CACHE_BOUNDARY -->\ndynamic")

    assert ws.sent[0]["session"] == {"instructions": "static\n\ndynamic"}
    _validate(ws.sent[0])


async def test_a_tools_offer_keeps_builtins_and_mcp_entries_around_the_new_function():
    adapter, ws = await _opened(builtin_search=True, servers=(_SERVER,))

    await adapter.update_session(tools=[_PAIR_TOOL])

    assert len(ws.sent) == 1
    session = ws.sent[0]["session"]
    assert session == {"tools": [*_BUILTINS, {"type": "function", **_PAIR_TOOL}, _entry(_SERVER)],
                       "tool_choice": "auto"}
    _validate(ws.sent[0], functions=["set_language_pair"], entries=[_entry(_SERVER)])


async def test_a_tools_offer_on_a_session_opened_without_tools():
    adapter, ws = await _opened()

    await adapter.update_session(tools=[_PAIR_TOOL])

    assert ws.sent[0]["session"] == {"tools": [{"type": "function", **_PAIR_TOOL}], "tool_choice": "auto"}
    _validate(ws.sent[0], functions=["set_language_pair"])


async def test_a_tools_update_replaces_the_function_tools_open_sent():
    adapter, ws = await _opened(tools=[_OTHER_TOOL])

    await adapter.update_session(tools=[_PAIR_TOOL])

    assert [t["name"] for t in ws.sent[0]["session"]["tools"]] == ["set_language_pair"]


async def test_a_withdraw_keeps_only_builtins_and_mcp_entries():
    adapter, ws = await _opened(builtin_search=True, servers=(_SERVER,))

    await adapter.update_session(tools=[])

    assert ws.sent[0]["session"] == {"tools": [*_BUILTINS, _entry(_SERVER)], "tool_choice": "auto"}
    _validate(ws.sent[0], entries=[_entry(_SERVER)])


async def test_a_withdraw_with_nothing_left_sends_an_empty_list_and_no_tool_choice():
    adapter, ws = await _opened()

    await adapter.update_session(tools=[])

    assert ws.sent == [{"type": "session.update", "session": {"tools": []}}]
    _validate(ws.sent[0])


async def test_instructions_and_tools_go_in_one_update():
    adapter, ws = await _opened()

    await adapter.update_session(instructions="base\n\nblock", tools=[_PAIR_TOOL])

    assert len(ws.sent) == 1
    assert ws.sent[0]["session"] == {"instructions": "base\n\nblock",
                                     "tools": [{"type": "function", **_PAIR_TOOL}], "tool_choice": "auto"}
    _validate(ws.sent[0], functions=["set_language_pair"])


async def test_nothing_to_change_sends_nothing():
    adapter, ws = await _opened()

    await adapter.update_session()

    assert ws.sent == []


@pytest.mark.parametrize("name", ["web_search", "x_search"])
async def test_a_function_named_like_a_builtin_is_refused_and_nothing_is_sent(name):
    adapter, ws = await _opened(builtin_search=True)

    with pytest.raises(ValueError, match=name):
        await adapter.update_session(instructions="base", tools=[{**_PAIR_TOOL, "name": name}])

    assert ws.sent == []


async def test_an_update_before_open_raises():
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock())

    with pytest.raises(RuntimeError):
        await adapter.update_session(tools=[])


class TestTheContractRuleCatches:
    """The rule itself fails a shape the adapter must never send."""

    def test_a_session_field_that_did_not_change(self):
        with pytest.raises(AssertionError):
            _validate({"type": "session.update", "session": {"instructions": "x", "voice": "rex"}})

    def test_a_tools_update_that_drops_the_mcp_entries(self):
        update = {"type": "session.update",
                  "session": {"tools": [{"type": "function", **_PAIR_TOOL}], "tool_choice": "auto"}}
        with pytest.raises(AssertionError):
            _validate(update, functions=["set_language_pair"], entries=[_entry(_SERVER)])

    def test_an_empty_tools_list_with_a_tool_choice(self):
        with pytest.raises(AssertionError):
            _validate({"type": "session.update", "session": {"tools": [], "tool_choice": "auto"}})
