"""TokenTextFastMCP: a FastMCP whose every word about its tools comes from Firestore tokens at request time
(DRIVE_MCP_CONNECTOR_RFC §5.3, §5.4, E15).

FastMCP registers its bound `list_tools` as the `tools/list` handler and builds the list from the tool manager on every
call (`mcp/server/fastmcp/server.py`, `_setup_handlers`, `list_tools`); the override here reads `ConnectorTextService`
(60 s cache) and, per registered tool, deep-copies the schema the SDK built from the handler's signature (types,
required, defaults: never touched), fills the tool's `description` and every `properties[p].description` from the
texts (`<tool>`, `<tool>.<param>`) and, for a parameter that is an array of inline objects, every item field's
`properties[p].items.properties[f].description` (`<tool>.<param>.<field>`, the memory inbox's `items`), marks the tool `readOnlyHint` when it is read-only (`read_only_tools`; None:
every tool, as the drive app's), and passes the tool's registered `_meta` through as
a copy. The registered tool objects are never mutated: they are shared by concurrent requests.

The server `instructions` (`INSTRUCTIONS_KEY`) come from the low-level server's `create_initialization_options`, which
the stateless transport calls once per request (`mcp/server/streamable_http_manager.py`). That call is synchronous, so
it serves the texts this app last read: loaded at boot (the builder awaits them before the app exists) and refreshed
by every `tools/list` and every tool call (`current_texts`). No `listChanged` is declared (E9): stateless HTTP cannot
deliver it.

A missing key raises KeyError while the list is built: the token's key set is checked at load, so it means a tool was
registered outside the contract, which the drive builder refuses at build.
"""
from __future__ import annotations

import copy
from typing import AbstractSet, Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.fastmcp.tools import Tool as RegisteredTool
from mcp.types import Tool as MCPTool
from mcp.types import ToolAnnotations

from ..domain.connector_texts import ConnectorTexts
from ..domain.drive_connector_contract import INSTRUCTIONS_KEY, text_key_of_parameter
from ..services.connector_text_service import ConnectorTextService


class TokenTextFastMCP(FastMCP):
    """`texts` serves the tool texts; `initial` is a value it already returned (the boot load); `read_only_tools` names
    the tools listed with `readOnlyHint` (None: every tool; a tool outside the set is listed with no annotations).
    Every other keyword is FastMCP's; `instructions` is not one: they come from the texts."""

    def __init__(self, *, texts: ConnectorTextService, initial: ConnectorTexts,
                 read_only_tools: Optional[AbstractSet[str]] = None, **settings: Any) -> None:
        super().__init__(**settings)
        self._text_service = texts
        self._latest = initial
        self._read_only_tools = frozenset(read_only_tools) if read_only_tools is not None else None
        built_in = self._mcp_server.create_initialization_options

        def initialization_options(notification_options: Optional[NotificationOptions] = None,
                                   experimental_capabilities: Optional[Dict[str, Dict[str, Any]]] = None,
                                   ) -> InitializationOptions:
            options = built_in(notification_options, experimental_capabilities)
            return options.model_copy(update={"instructions": self._latest.tools[INSTRUCTIONS_KEY]})

        # The low-level server is FastMCP's own instance; its options method is replaced on that instance only.
        self._mcp_server.create_initialization_options = initialization_options  # type: ignore[method-assign]

    async def current_texts(self) -> ConnectorTexts:
        """The texts now (`ConnectorTextService.texts()`), remembered for the next initialization's instructions.
        Raises ConnectorTextsUnavailable only while no load ever succeeded."""
        texts = await self._text_service.texts()
        self._latest = texts
        return texts

    async def list_tools(self) -> list[MCPTool]:
        texts = await self.current_texts()
        return [_listed(info, texts, self._read_only_tools is None or info.name in self._read_only_tools)
                for info in self._tool_manager.list_tools()]


def _listed(info: RegisteredTool, texts: ConnectorTexts, read_only: bool) -> MCPTool:
    schema = copy.deepcopy(info.parameters)
    for name, prop in schema.get("properties", {}).items():
        key = text_key_of_parameter(info.name, name)
        prop["description"] = texts.tools[key]
        item = prop.get("items") if prop.get("type") == "array" else None
        if isinstance(item, dict) and item.get("type") == "object":
            for field, field_schema in item.get("properties", {}).items():
                field_schema["description"] = texts.tools[f"{key}.{field}"]
    return MCPTool(
        name=info.name,
        title=info.title,
        description=texts.tools[info.name],
        inputSchema=schema,
        outputSchema=copy.deepcopy(info.output_schema),
        annotations=ToolAnnotations(readOnlyHint=True) if read_only else None,
        icons=info.icons,
        _meta=copy.deepcopy(info.meta),
    )
