"""
Wire tests for MCPClient + MCPMapsAdapter.

Mock boundary: aiohttp.ClientSession (SDK boundary — not the port).
Tests verify:
  - Correct JSON-RPC envelope construction for initialize and tools/call
  - Correct X-Goog-Api-Key header
  - Tool schema conversion (MCP inputSchema → FunctionDeclaration dict)
  - Tool result parsing (JSON string in content[0].text)
  - Tool caching (initialize called once per client instance)
  - Error handling: HTTP error, JSON-RPC error, MCPToolError propagation
  - MCPMapsAdapter: MapsToolError wrapping of MCPToolError

Per ADAPTER_WIRE_TESTING.md: mock at aiohttp, not at port level.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.mcp.mcp_client import MCPClient, MCPToolError
from src.adapters.mcp.mcp_maps_adapter import MCPMapsAdapter
from src.ports.maps_tools_port import MapsToolError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MCP_URL = "https://mapstools.googleapis.com/mcp"
_API_KEY = "test-api-key-123"

_INITIALIZE_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "tools": [
            {
                "name": "places_search",
                "description": "Search for places.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "route_computation",
                "description": "Compute a route.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "origin": {"type": "string"},
                        "destination": {"type": "string"},
                    },
                    "required": ["origin", "destination"],
                },
            },
            {
                "name": "weather_lookup",
                "description": "Get weather.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        ]
    },
}

_PLACES_RESULT = {
    "places": [
        {"name": "Аптека 24", "address": "вул. Хрещатик, 1", "rating": 4.5}
    ]
}

_TOOL_CALL_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "content": [{"type": "text", "text": json.dumps(_PLACES_RESULT)}]
    },
}


def _make_aiohttp_mock(json_response: dict, status: int = 200):
    """Build a mock aiohttp.ClientSession that returns the given JSON response."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_response)
    mock_resp.text = AsyncMock(return_value=json.dumps(json_response))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


# ---------------------------------------------------------------------------
# MCPClient — initialize (tool discovery)
# ---------------------------------------------------------------------------

class TestMCPClientInitialize:
    async def test_returns_converted_tool_declarations(self):
        with patch("aiohttp.ClientSession", return_value=_make_aiohttp_mock(_INITIALIZE_RESPONSE)):
            client = MCPClient(_MCP_URL, _API_KEY)
            decls = await client.get_tool_declarations()

        assert len(decls) == 3
        names = {d["name"] for d in decls}
        assert names == {"places_search", "route_computation", "weather_lookup"}

    async def test_inputschema_converted_to_parameters(self):
        with patch("aiohttp.ClientSession", return_value=_make_aiohttp_mock(_INITIALIZE_RESPONSE)):
            client = MCPClient(_MCP_URL, _API_KEY)
            decls = await client.get_tool_declarations()

        places = next(d for d in decls if d["name"] == "places_search")
        assert "parameters" in places
        assert "inputSchema" not in places
        assert places["parameters"]["properties"]["query"]["type"] == "string"

    async def test_api_key_sent_in_header(self):
        mock_session = _make_aiohttp_mock(_INITIALIZE_RESPONSE)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = MCPClient(_MCP_URL, _API_KEY)
            await client.get_tool_declarations()

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else {}
        # headers passed as kwarg
        headers = mock_session.post.call_args.kwargs.get("headers", {})
        assert headers.get("X-Goog-Api-Key") == _API_KEY

    async def test_initialize_called_only_once(self):
        mock_session = _make_aiohttp_mock(_INITIALIZE_RESPONSE)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = MCPClient(_MCP_URL, _API_KEY)
            await client.get_tool_declarations()
            await client.get_tool_declarations()  # second call — should use cache

        # aiohttp.ClientSession() called once (for the single initialize POST)
        assert mock_session.post.call_count == 1

    async def test_initialize_jsonrpc_envelope(self):
        mock_session = _make_aiohttp_mock(_INITIALIZE_RESPONSE)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = MCPClient(_MCP_URL, _API_KEY)
            await client.get_tool_declarations()

        payload = mock_session.post.call_args.kwargs["json"]
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "tools/list"
        assert "params" in payload


# ---------------------------------------------------------------------------
# MCPClient — call_tool
# ---------------------------------------------------------------------------

class TestMCPClientCallTool:
    async def test_returns_parsed_json_result(self):
        with patch("aiohttp.ClientSession", return_value=_make_aiohttp_mock(_TOOL_CALL_RESPONSE)):
            client = MCPClient(_MCP_URL, _API_KEY)
            result = await client.call_tool("places_search", {"query": "аптека"})

        assert result == _PLACES_RESULT

    async def test_correct_jsonrpc_envelope(self):
        mock_session = _make_aiohttp_mock(_TOOL_CALL_RESPONSE)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = MCPClient(_MCP_URL, _API_KEY)
            await client.call_tool("places_search", {"query": "кафе"})

        payload = mock_session.post.call_args.kwargs["json"]
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "tools/call"
        assert payload["params"]["name"] == "places_search"
        assert payload["params"]["arguments"] == {"query": "кафе"}

    async def test_http_error_raises_mcp_tool_error(self):
        mock_session = _make_aiohttp_mock({}, status=429)
        mock_session.post.return_value.text = AsyncMock(return_value="Rate limit exceeded")
        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = MCPClient(_MCP_URL, _API_KEY)
            with pytest.raises(MCPToolError, match="HTTP 429"):
                await client.call_tool("places_search", {"query": "x"})

    async def test_jsonrpc_error_raises_mcp_tool_error(self):
        error_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32001, "message": "Quota exceeded"},
        }
        with patch("aiohttp.ClientSession", return_value=_make_aiohttp_mock(error_response)):
            client = MCPClient(_MCP_URL, _API_KEY)
            with pytest.raises(MCPToolError, match="Quota exceeded"):
                await client.call_tool("places_search", {"query": "x"})

    async def test_non_json_text_result_wrapped(self):
        """Plain text result (non-JSON) wrapped in {text: ...} dict."""
        text_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "plain text result"}]},
        }
        with patch("aiohttp.ClientSession", return_value=_make_aiohttp_mock(text_response)):
            client = MCPClient(_MCP_URL, _API_KEY)
            result = await client.call_tool("weather_lookup", {"location": "Kyiv"})

        assert result == {"text": "plain text result"}


# ---------------------------------------------------------------------------
# MCPMapsAdapter — port contract + error wrapping
# ---------------------------------------------------------------------------

class TestMCPMapsAdapter:
    async def test_get_tool_declarations_delegates_to_client(self):
        mock_client = AsyncMock(spec=MCPClient)
        mock_client.get_tool_declarations.return_value = [{"name": "places_search"}]

        adapter = MCPMapsAdapter(mock_client)
        decls = await adapter.get_tool_declarations()

        assert decls == [{"name": "places_search"}]
        mock_client.get_tool_declarations.assert_awaited_once()

    async def test_call_tool_delegates_to_client(self):
        mock_client = AsyncMock(spec=MCPClient)
        mock_client.call_tool.return_value = {"places": []}

        adapter = MCPMapsAdapter(mock_client)
        result = await adapter.call_tool("places_search", {"query": "кафе"})

        assert result == {"places": []}
        mock_client.call_tool.assert_awaited_once_with("places_search", {"query": "кафе"})

    async def test_mcp_tool_error_wrapped_as_maps_tool_error(self):
        mock_client = AsyncMock(spec=MCPClient)
        mock_client.call_tool.side_effect = MCPToolError("quota exceeded")

        adapter = MCPMapsAdapter(mock_client)
        with pytest.raises(MapsToolError, match="quota exceeded"):
            await adapter.call_tool("places_search", {"query": "x"})

    def test_capability_description_is_non_empty_string(self):
        assert isinstance(MCPMapsAdapter.CAPABILITY_DESCRIPTION, str)
        assert len(MCPMapsAdapter.CAPABILITY_DESCRIPTION) > 20


# ---------------------------------------------------------------------------
# MCPMapsAdapter — argument normalization (protobuf-zero → omit impedance match)
# ---------------------------------------------------------------------------

class TestMCPMapsAdapterArgNormalization:
    """The adapter strips LLM-emitted placeholder zeros that the Maps REST tools
    reject: a {0,0,0} date and over-filled oneof location sub-fields (latLng {0,0})."""

    # -- weather: date ------------------------------------------------------

    def test_weather_zero_date_and_hour_dropped(self):
        out = MCPMapsAdapter._normalize_arguments(
            "lookup_weather",
            {"location": {"address": "Kyiv, UA"}, "date": {"day": 0, "month": 0, "year": 0}, "hour": 0},
        )
        assert "date" not in out
        assert "hour" not in out
        assert out["location"] == {"address": "Kyiv, UA"}

    def test_weather_partial_date_dropped(self):
        # day=0 is a protobuf placeholder, not a usable weather date.
        out = MCPMapsAdapter._normalize_arguments(
            "lookup_weather",
            {"location": {"address": "Kyiv, UA"}, "date": {"day": 0, "month": 6, "year": 2026}},
        )
        assert "date" not in out

    def test_weather_valid_date_and_hour_kept(self):
        out = MCPMapsAdapter._normalize_arguments(
            "lookup_weather",
            {"location": {"address": "Kyiv, UA"}, "date": {"day": 15, "month": 7, "year": 2026}, "hour": 14},
        )
        assert out["date"] == {"day": 15, "month": 7, "year": 2026}
        assert out["hour"] == 14

    # -- weather: location oneof -------------------------------------------

    def test_location_prefers_latlng_and_drops_others(self):
        out = MCPMapsAdapter._normalize_arguments(
            "lookup_weather",
            {"location": {
                "address": "Kyiv, UA",
                "latLng": {"latitude": 50.45, "longitude": 30.52},
                "placeId": "abc",
            }},
        )
        assert out["location"] == {"latLng": {"latitude": 50.45, "longitude": 30.52}}

    def test_location_zero_latlng_falls_back_to_placeid(self):
        out = MCPMapsAdapter._normalize_arguments(
            "lookup_weather",
            {"location": {"address": "", "latLng": {"latitude": 0, "longitude": 0}, "placeId": "ChIJxxx"}},
        )
        assert out["location"] == {"placeId": "ChIJxxx"}

    def test_location_empty_falls_back_to_address(self):
        out = MCPMapsAdapter._normalize_arguments(
            "lookup_weather",
            {"location": {"address": "Odesa, UA", "latLng": {"latitude": 0, "longitude": 0}, "placeId": ""}},
        )
        assert out["location"] == {"address": "Odesa, UA"}

    def test_location_nothing_usable_returned_untouched(self):
        loc = {"address": "  ", "latLng": {"latitude": 0, "longitude": 0}, "placeId": ""}
        out = MCPMapsAdapter._normalize_arguments("lookup_weather", {"location": loc})
        assert out["location"] == loc

    # -- routes: waypoint oneof --------------------------------------------

    def test_routes_waypoints_cleaned(self):
        out = MCPMapsAdapter._normalize_arguments(
            "compute_routes",
            {
                "origin": {"address": "Kyiv", "latLng": {"latitude": 0, "longitude": 0}},
                "destination": {"address": "Lviv", "latLng": {"latitude": 49.84, "longitude": 24.03}},
                "travelMode": "DRIVE",
            },
        )
        assert out["origin"] == {"address": "Kyiv"}
        assert out["destination"] == {"latLng": {"latitude": 49.84, "longitude": 24.03}}
        assert out["travelMode"] == "DRIVE"

    # -- passthrough / safety ----------------------------------------------

    def test_search_places_passthrough_unchanged(self):
        args = {"textQuery": "pharmacy in Kyiv", "regionCode": "UA"}
        out = MCPMapsAdapter._normalize_arguments("search_places", args)
        assert out == args

    def test_unknown_tool_passthrough_unchanged(self):
        args = {"foo": {"day": 0, "month": 0, "year": 0}}
        out = MCPMapsAdapter._normalize_arguments("resolve_maps_urls", args)
        assert out == args

    def test_input_dict_not_mutated(self):
        original = {"location": {"address": "Kyiv", "latLng": {"latitude": 0, "longitude": 0}},
                    "date": {"day": 0, "month": 0, "year": 0}, "hour": 0}
        snapshot = json.loads(json.dumps(original))
        MCPMapsAdapter._normalize_arguments("lookup_weather", original)
        assert original == snapshot

    async def test_call_tool_forwards_normalized_args(self):
        mock_client = AsyncMock(spec=MCPClient)
        mock_client.call_tool.return_value = {"ok": True}

        adapter = MCPMapsAdapter(mock_client)
        await adapter.call_tool(
            "lookup_weather",
            {"location": {"address": "Kyiv, UA"}, "date": {"day": 0, "month": 0, "year": 0}, "hour": 0},
        )

        mock_client.call_tool.assert_awaited_once_with(
            "lookup_weather", {"location": {"address": "Kyiv, UA"}}
        )
