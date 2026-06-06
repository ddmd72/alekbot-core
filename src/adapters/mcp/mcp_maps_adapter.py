"""
MCPMapsAdapter — Google Maps AI Grounding Lite via MCP
======================================================

Implements MapsToolsPort using MCPClient as transport.

MCP server: https://mapstools.googleapis.com/mcp
Auth: X-Goog-Api-Key (reuses GOOGLE_SEARCH_API_KEY)

Tools exposed:
    places_search       — place discovery, addresses, ratings, hours
    route_computation   — distance + duration (no turn-by-turn, no real-time traffic)
    weather_lookup      — current conditions, hourly/daily forecast

Backend switchability:
    To revert to Gemini native grounding, create GeminiMapsAdapter(MapsToolsPort)
    and swap in user_agent_factory.py. Update CAPABILITY_DESCRIPTION text in
    agent_manifest.py accordingly. MapsSearchAgent itself does not change.

RFC: docs/10_rfcs/MCP_INFRASTRUCTURE_RFC.md § 6
"""

from ...ports.maps_tools_port import MapsToolsPort, MapsToolError
from .mcp_client import MCPClient, MCPToolError as _MCPToolError


class MCPMapsAdapter(MapsToolsPort):
    """
    MapsToolsPort implementation backed by Google Maps AI Grounding Lite MCP server.

    CAPABILITY_DESCRIPTION is a class-level constant consumed by agent_manifest.py.
    Update this string (and the manifest) when switching backends.
    """

    CAPABILITY_DESCRIPTION = (
        "Place search and discovery, route computation (distance and duration — "
        "not turn-by-turn directions or real-time traffic), and current weather "
        "lookup via Google Maps AI Grounding Lite. "
        "Input: natural language task. "
        "Examples: 'pharmacy near Khreschatyk open now', "
        "'route from Kyiv to Lviv by car', 'weather in Odesa today'."
    )

    # Tools whose Google schema encodes a protobuf `oneof` / "zero = unspecified"
    # convention that weaker models fill with placeholder zeros (see _normalize_arguments).
    _WEATHER_TOOL = "lookup_weather"
    _ROUTES_TOOL = "compute_routes"

    def __init__(self, mcp_client: MCPClient) -> None:
        self._client = mcp_client

    async def get_tool_declarations(self) -> list[dict]:
        """Return MCP tool schemas as LLMRequest-compatible FunctionDeclaration dicts."""
        return await self._client.get_tool_declarations()

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Execute a Maps tool via MCP and return the parsed result."""
        arguments = self._normalize_arguments(name, arguments)
        try:
            return await self._client.call_tool(name, arguments)
        except _MCPToolError as exc:
            raise MapsToolError(str(exc)) from exc

    # -- Argument normalization ----------------------------------------------------
    # Impedance match between LLM output and the Google Maps REST tools. Two schema
    # quirks defeat even a correct tool description on weaker models (e.g. Flash):
    #   - `location` is a protobuf `oneof` (latLng|placeId|address) flattened by JSON
    #     Schema to a plain object, so the model fills several sub-fields, padding the
    #     unused `latLng` with {0,0}.
    #   - `date` follows the protobuf "zero = unspecified" convention, so the model
    #     sends {0,0,0} to mean "no date" instead of omitting it — but the weather
    #     endpoint rejects a zero date rather than treating it as "current".
    # We translate those placeholder zeros into the omit-the-field semantics the wire
    # expects. Non-maps tools and already well-formed args pass through unchanged. The
    # input dict is never mutated (tool calls run concurrently and stay in history).

    @classmethod
    def _normalize_arguments(cls, name: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            return arguments
        if name == cls._WEATHER_TOOL:
            return cls._normalize_weather(arguments)
        if name == cls._ROUTES_TOOL:
            return cls._normalize_routes(arguments)
        return arguments

    @classmethod
    def _normalize_weather(cls, args: dict) -> dict:
        out = dict(args)
        if "location" in out:
            out["location"] = cls._clean_location(out["location"])
        # A zero/partial date means "no specific date" → drop date (+hour) so the tool
        # runs in current-weather mode (location-only), which is what the model intended.
        if not cls._is_usable_date(out.get("date")):
            out.pop("date", None)
            out.pop("hour", None)
        return out

    @classmethod
    def _normalize_routes(cls, args: dict) -> dict:
        out = dict(args)
        for waypoint in ("origin", "destination"):
            if waypoint in out:
                out[waypoint] = cls._clean_location(out[waypoint])
        return out

    @staticmethod
    def _clean_location(location: dict) -> dict:
        """Collapse a Maps oneof location to its single most precise usable field.

        Priority latLng > placeId > address; zero/empty sub-fields are dropped. (0,0)
        is treated as a placeholder, not a real coordinate. If no field is usable the
        original object is returned untouched — let the server respond rather than
        fabricate a value.
        """
        if not isinstance(location, dict):
            return location
        lat_lng = location.get("latLng")
        if isinstance(lat_lng, dict):
            lat, lng = lat_lng.get("latitude"), lat_lng.get("longitude")
            if (
                isinstance(lat, (int, float))
                and isinstance(lng, (int, float))
                and not (lat == 0 and lng == 0)
            ):
                return {"latLng": {"latitude": lat, "longitude": lng}}
        place_id = location.get("placeId")
        if isinstance(place_id, str) and place_id.strip():
            return {"placeId": place_id}
        address = location.get("address")
        if isinstance(address, str) and address.strip():
            return {"address": address}
        return location

    @staticmethod
    def _is_usable_date(date: object) -> bool:
        """True only for a full calendar date (day 1-31, month 1-12, year >= 1).

        Protobuf zero-placeholders ({0,0,0} or partial) are not usable for a weather
        lookup, which needs a concrete day.
        """
        if not isinstance(date, dict):
            return False
        day, month, year = date.get("day", 0), date.get("month", 0), date.get("year", 0)
        return (
            isinstance(day, int) and 1 <= day <= 31
            and isinstance(month, int) and 1 <= month <= 12
            and isinstance(year, int) and year >= 1
        )
