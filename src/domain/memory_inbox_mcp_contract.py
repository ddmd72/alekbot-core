"""The memory inbox MCP resource's tool contract (MCP_MEMORY_SUBMISSIONS_RFC §5.2, §5.3): its one tool, its one
parameter, the item fields, the keys its two Firestore tokens must carry, and the item schema it advertises.

The words live in the tokens `MCP_MEMORY_INBOX_TOOL_TEXTS` and `MCP_MEMORY_INBOX_RESULT_TEXTS`; this module only fixes
their key sets, which `ConnectorTexts.parse_bodies` checks at load (a missing key is an error, an unknown one is logged
and ignored). The tool token uses the drive connector's key shapes (`instructions`, `<tool>`, `<tool>.<param>`) plus one
key per item field (`<tool>.<param>.<field>`), which `TokenTextFastMCP` fills into the listed item schema.

The tool takes no user and no account: both come from the verified token.

Result keys, each a line; a success answers `RECEIVED`, then `CUT` when anything was cut and `OVER_DAY` when anything
was over the day's cap; the error lines are `NOTHING_USABLE` (no item carried information), `STORE_FAILED` and
`REFUSED` (the token's grant is no longer allowed: revoked in the Cabinet, or its user off the whitelist).
`RESULT_PLACEHOLDERS` names the `{placeholders}` code fills in each; none carries submitted text.

`ITEM_SCHEMA` is the advertised item: one inline object of four strings, only `information` required, no constraint
(no `maxLength`, no `additionalProperties`; `decisions/mcp_tool_schema_liberal_input.md`). Pure: no I/O.
"""
from __future__ import annotations

import copy
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Mapping, Tuple

from .drive_connector_contract import INSTRUCTIONS_KEY, text_key_of_parameter

# -- the tool ---------------------------------------------------------------------------------------------------------
TOOL = "submit_user_information"
PARAM = "items"
ITEM_FIELDS: Tuple[str, ...] = ("information", "basis", "said_when", "happens_when")

TOOL_PARAMETERS: Mapping[str, Tuple[str, ...]] = MappingProxyType({TOOL: (PARAM,)})


def text_key_of_item_field(field: str) -> str:
    """The key of one item field's text: `submit_user_information.items.basis`."""
    return f"{text_key_of_parameter(TOOL, PARAM)}.{field}"


REQUIRED_TOOL_KEYS: FrozenSet[str] = frozenset(
    {INSTRUCTIONS_KEY, TOOL, text_key_of_parameter(TOOL, PARAM), *(text_key_of_item_field(f) for f in ITEM_FIELDS)})

ITEM_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in ITEM_FIELDS},
    "required": ["information"],
}


def items_schema() -> Dict[str, Any]:
    """The advertised `items` parameter: an array of inline ITEM_SCHEMA objects (a fresh copy each call)."""
    return {"type": "array", "items": copy.deepcopy(dict(ITEM_SCHEMA))}


# -- results ----------------------------------------------------------------------------------------------------------
RECEIVED = "RECEIVED"
CUT = "CUT"
OVER_DAY = "OVER_DAY"
NOTHING_USABLE = "NOTHING_USABLE"
STORE_FAILED = "STORE_FAILED"
REFUSED = "REFUSED"

RESULT_PLACEHOLDERS: Mapping[str, FrozenSet[str]] = MappingProxyType({
    RECEIVED: frozenset({"received"}),
    CUT: frozenset({"cut"}),
    OVER_DAY: frozenset({"over_day"}),
    NOTHING_USABLE: frozenset(),
    STORE_FAILED: frozenset(),
    REFUSED: frozenset(),
})

REQUIRED_RESULT_KEYS: FrozenSet[str] = frozenset(RESULT_PLACEHOLDERS)
