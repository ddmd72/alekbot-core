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

    def __init__(self, mcp_client: MCPClient) -> None:
        self._client = mcp_client

    async def get_tool_declarations(self) -> list[dict]:
        """Return MCP tool schemas as LLMRequest-compatible FunctionDeclaration dicts."""
        return await self._client.get_tool_declarations()

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Execute a Maps tool via MCP and return the parsed result."""
        try:
            return await self._client.call_tool(name, arguments)
        except _MCPToolError as exc:
            raise MapsToolError(str(exc)) from exc
