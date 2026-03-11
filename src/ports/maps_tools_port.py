"""
Maps Tools Port
===============

Abstract port for location services: place search, route computation, weather lookup.

Implementations:
  MCPMapsAdapter  — Google Maps AI Grounding Lite via MCP protocol (current)
  GeminiMapsAdapter — Gemini native types.GoogleMaps grounding (legacy fallback)

RFC: docs/10_rfcs/MCP_INFRASTRUCTURE_RFC.md
"""

from abc import ABC, abstractmethod


class MapsToolsPort(ABC):
    """Port for map-related tool capabilities."""

    @abstractmethod
    async def get_tool_declarations(self) -> list[dict]:
        """
        Return tool schemas in LLMRequest-compatible FunctionDeclaration dict format.

        Each dict must have:
            {
                "name": str,
                "description": str,
                "parameters": {  # JSON Schema
                    "type": "object",
                    "properties": {...},
                    "required": [...],
                }
            }

        GeminiAdapter converts these to types.FunctionDeclaration automatically.
        ClaudeAdapter maps to its native tool format.
        """

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict) -> dict:
        """
        Execute a named tool call and return the parsed JSON result.

        Args:
            name: Tool name (e.g. "places_search", "route_computation", "weather_lookup")
            arguments: Tool arguments as a dict matching the tool's input schema.

        Returns:
            Parsed JSON dict from the tool response.

        Raises:
            MapsToolError: On tool execution failure (network error, quota exceeded, etc.)
        """


class MapsToolError(Exception):
    """Raised when a map tool call fails."""
