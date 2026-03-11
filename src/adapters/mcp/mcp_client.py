"""
MCPClient — Generic MCP Protocol Transport
==========================================

Private infrastructure helper for adapters that communicate with MCP servers.
Not a port. Not for use outside adapters/mcp/.

Implements JSON-RPC 2.0 over Streamable HTTP (POST-only, stateless).
Tool schemas fetched via `initialize` are cached for the lifetime of the instance.

Usage:
    client = MCPClient(
        base_url="https://mapstools.googleapis.com/mcp",
        api_key=config["GOOGLE_SEARCH_API_KEY"],
    )
    declarations = await client.get_tool_declarations()
    result = await client.call_tool("places_search", {"query": "pharmacy near Kyiv"})

RFC: docs/10_rfcs/MCP_INFRASTRUCTURE_RFC.md § 5
"""

import json
import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"


class MCPToolError(Exception):
    """Raised when an MCP tool call fails (network error, server error, quota)."""


class MCPClient:
    """
    Minimal MCP client: initialize (tool discovery) + tools/call (tool execution).

    Thread-safe for concurrent tool calls after initialization.
    Tool schemas are immutable per server version — cached after first fetch.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._tool_declarations: Optional[list[dict]] = None

    async def get_tool_declarations(self) -> list[dict]:
        """
        Return tool schemas in LLMRequest-compatible FunctionDeclaration dict format.

        Fetches from server on first call; returns cached result on subsequent calls.
        Converts MCP inputSchema → dict with "name", "description", "parameters".
        """
        if self._tool_declarations is not None:
            return self._tool_declarations

        raw_tools = await self._initialize()
        self._tool_declarations = [self._convert_tool_schema(t) for t in raw_tools]
        logger.info(
            f"[MCPClient] Initialized: {[t['name'] for t in self._tool_declarations]}"
        )
        return self._tool_declarations

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """
        Execute a named MCP tool and return the parsed JSON result.

        Raises:
            MCPToolError: On HTTP error, JSON-RPC error, or unexpected response format.
        """
        payload = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }
        response_data = await self._post(payload)
        return self._parse_tool_result(name, response_data)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _initialize(self) -> list[dict]:
        """
        Fetch tool list from MCP server.

        MCP protocol has two separate steps:
          1. initialize  — session handshake; returns server capabilities (NOT tool list)
          2. tools/list  — returns the actual list of available tools

        We skip the initialize handshake (stateless HTTP server does not require it)
        and call tools/list directly to get the tool schemas.
        """
        payload = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        data = await self._post(payload)
        result = data.get("result", {})
        tools = result.get("tools") or []
        if not tools:
            logger.warning(
                f"[MCPClient] tools/list returned no tools. Full response: {data!r}"
            )
        return tools if isinstance(tools, list) else []

    async def _post(self, payload: dict) -> dict:
        """POST JSON-RPC payload to MCP server. Returns parsed response dict."""
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._base_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise MCPToolError(
                            f"MCP server returned HTTP {resp.status}: {body[:200]}"
                        )
                    return await resp.json()
        except aiohttp.ClientError as exc:
            raise MCPToolError(f"MCP HTTP error: {exc}") from exc

    @staticmethod
    def _convert_tool_schema(raw_tool: dict) -> dict:
        """
        Convert MCP tool schema to LLMRequest-compatible FunctionDeclaration dict.

        MCP format:
            {name, description, inputSchema: {type, properties, required, $defs, ...}}

        Output format (matches GeminiAdapter._convert_tools_to_sdk_format + ClaudeAdapter):
            {name, description, parameters: {type, properties, required}}

        Also dereferences JSON Schema $ref/$defs and strips Gemini-unsupported fields
        (format, x-google-enum-descriptions, $schema) that cause MALFORMED_FUNCTION_CALL.
        """
        raw_schema = raw_tool.get("inputSchema") or {}
        return {
            "name": raw_tool.get("name", ""),
            "description": raw_tool.get("description", ""),
            "parameters": MCPClient._deref_schema(raw_schema),
        }

    @staticmethod
    def _deref_schema(schema: dict) -> dict:
        """
        Inline JSON Schema $ref references from $defs and strip unsupported fields.

        Gemini's types.Schema does not support $ref, $defs, format,
        x-google-enum-descriptions, or $schema. Inlining resolves nested object
        references (e.g. LatLng, Waypoint, Circle) into flat property trees.
        """
        defs: dict = schema.get("$defs", {})

        _UNSUPPORTED = frozenset({
            "$defs", "$schema", "$id", "format",
            "x-google-enum-descriptions", "x-google-enum-descriptions-long",
        })

        def resolve(node: Any) -> Any:
            if isinstance(node, dict):
                if "$ref" in node:
                    ref: str = node["$ref"]  # e.g. "#/$defs/LatLng"
                    def_name = ref.split("/")[-1]
                    return resolve(defs.get(def_name, {}))
                return {
                    k: resolve(v)
                    for k, v in node.items()
                    if k not in _UNSUPPORTED
                }
            if isinstance(node, list):
                return [resolve(item) for item in node]
            return node

        return resolve(schema)

    @staticmethod
    def _parse_tool_result(name: str, data: dict) -> dict:
        """
        Extract tool result from MCP tools/call response.

        MCP response structure:
            {result: {content: [{type: "text", text: "<JSON string>"}]}}

        Returns parsed JSON dict. On parse failure returns raw text wrapped in dict.
        """
        if "error" in data:
            err = data["error"]
            raise MCPToolError(
                f"MCP tool '{name}' error {err.get('code')}: {err.get('message')}"
            )

        result = data.get("result", {})
        content = result.get("content", [])

        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed: Any = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                    return {"result": parsed}
                except (json.JSONDecodeError, TypeError):
                    return {"text": text}

        logger.warning(f"[MCPClient] Unexpected tool result format for '{name}': {data!r}")
        return {"raw": data}
