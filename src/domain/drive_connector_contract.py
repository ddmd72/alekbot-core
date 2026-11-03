"""The drive connector's tool contract: its tool names, each tool's parameter names, and the keys its two Firestore
tokens must carry (DRIVE_MCP_CONNECTOR_RFC §4.1, §5.2, §5.5, §6.2, §7.7).

The words live in the tokens `MCP_DRIVE_TOOL_TEXTS` and `MCP_DRIVE_RESULT_TEXTS`; this module only fixes their key
sets, which `ConnectorTexts.from_bodies` checks for equality (a missing or unknown key is an error).

The tool names are the chat tools' names on purpose: result texts reused as they are name them (the passages cut line
"read_passages from passage N continues", the open bound's route), so one name serves both surfaces. The parameters
mirror the chat tools' parameters; none is dropped, since the chat's read tools take no `file_contents` item and no
specialist-only field.

Result keys:
- the reading lines by the names `drive_answer.reading_keys` emits (the keys of `CHAT_READING_TEXTS`);
- one line per `Withheld` code (`W_IMAGE`, …), printed as is;
- the status and what-to-do lines, the unfinished line and the passages' language line (the chat's `STATUS_HEAD`,
  `WHAT_TO_DO`, `ANSWER_FROM_RESULTS`, `UNFINISHED_LINE`, `LANGUAGE_LINE` re-voiced);
- `ROUTE` after an open refused as too long (in place of `ROUTE_SPECIALIST`), `PAGE_NEXT` (in place of the excerpt
  renderer's `PAGE_NEXT`), `REFUSED` (the §13 refusal line) and `REFUSED_GUIDANCE` (its companion guidance, §5.2),
  `OVER_SIZE` (the line the 100,000-character guard appends, §6.1).

`RESULT_PLACEHOLDERS` names the `{placeholders}` code fills in a result text; a key it does not list is printed as is.
Pure: no I/O.

Public interface:
    TOOL_NAMES, TOOL_PARAMETERS                     the tools, in their listing order, and each tool's parameter names
    INSTRUCTIONS_KEY, text_key_of_parameter(tool, param)    the server-instructions key; a parameter text's key ("tool.param")
    READING_KEYS, text_key_of_withheld(code), WITHHELD_KEYS the reading-line keys; a withheld code's key
    REQUIRED_TOOL_KEYS, REQUIRED_RESULT_KEYS        the exact key sets of the two tokens
    RESULT_PLACEHOLDERS                             key -> the placeholder names code formats it with
"""
from __future__ import annotations

from types import MappingProxyType
from typing import FrozenSet, Mapping, Tuple

from .drive_answer_texts import CHAT_READING_TEXTS
from .drive_note_tool_texts import GET_NOTE
from .drive_tool_texts import FIND_PASSAGES, LIST_FOLDER, OPEN_DRIVE_FILE, READ_PASSAGES, SEARCH_DRIVE
from .drive_withheld import Withheld

# -- tools ------------------------------------------------------------------------------------------------------------
# get_download_link comes with the one-time link (RFC §8, slice 4); it is not part of this contract yet.
TOOL_NAMES: Tuple[str, ...] = (SEARCH_DRIVE, FIND_PASSAGES, READ_PASSAGES, OPEN_DRIVE_FILE, LIST_FOLDER, GET_NOTE)

TOOL_PARAMETERS: Mapping[str, Tuple[str, ...]] = MappingProxyType({
    SEARCH_DRIVE: ("query", "folder", "drives", "kind", "date_from", "date_to"),
    FIND_PASSAGES: ("question", "folder", "drives", "file_refs"),
    READ_PASSAGES: ("file_ref", "passages"),
    OPEN_DRIVE_FILE: ("file_ref",),
    LIST_FOLDER: ("folder",),
    # whole_drive_name stays: a whole-drive note is readable for a drive the connector sees (RFC §7.5)
    GET_NOTE: ("item_ref", "whole_drive_name"),
})

INSTRUCTIONS_KEY = "instructions"


def text_key_of_parameter(tool: str, param: str) -> str:
    """The key of one parameter's text in `MCP_DRIVE_TOOL_TEXTS`: `search_drive.query`."""
    return f"{tool}.{param}"


REQUIRED_TOOL_KEYS: FrozenSet[str] = frozenset(
    {INSTRUCTIONS_KEY, *TOOL_NAMES,
     *(text_key_of_parameter(tool, param) for tool, params in TOOL_PARAMETERS.items() for param in params)})

# -- results ----------------------------------------------------------------------------------------------------------
READING_KEYS: Tuple[str, ...] = tuple(CHAT_READING_TEXTS)


def text_key_of_withheld(code: Withheld) -> str:
    """The key of the line an open withheld for `code` answers with: `W_IMAGE`, `W_RECORDING_UNTRANSCRIBED`, …"""
    return f"W_{code.name}"


WITHHELD_KEYS: Tuple[str, ...] = tuple(text_key_of_withheld(code) for code in Withheld)

STATUS = "STATUS"                              # "Drive result: {status}"
WHAT_TO_DO = "WHAT_TO_DO"                      # "What to do: {line}"
ANSWER_FROM_RESULTS = "ANSWER_FROM_RESULTS"    # the what-to-do of a read that returned content
UNFINISHED = "UNFINISHED"                      # a call that did not finish in time
LANGUAGE = "LANGUAGE"                          # after a passages block
ROUTE = "ROUTE"                                # after an open refused as too long; {ref} is the file's drive: ref
PAGE_NEXT = "PAGE_NEXT"                        # a page that is not the file's last
REFUSED = "REFUSED"                            # outside the connector's folders, or no such item (RFC §7.7, §13)
REFUSED_GUIDANCE = "REFUSED_GUIDANCE"          # what to do after a refusal
OVER_SIZE = "OVER_SIZE"                        # the result was cut at the connector's hard size guard

LINE_KEYS: Tuple[str, ...] = (STATUS, WHAT_TO_DO, ANSWER_FROM_RESULTS, UNFINISHED, LANGUAGE, ROUTE, PAGE_NEXT,
                              REFUSED, REFUSED_GUIDANCE, OVER_SIZE)

REQUIRED_RESULT_KEYS: FrozenSet[str] = frozenset({*READING_KEYS, *WITHHELD_KEYS, *LINE_KEYS})

# The same placeholder names as the chat constants these texts replace, so code formats either with one call.
RESULT_PLACEHOLDERS: Mapping[str, FrozenSet[str]] = MappingProxyType({
    STATUS: frozenset({"status"}),
    WHAT_TO_DO: frozenset({"line"}),
    ROUTE: frozenset({"ref"}),
    PAGE_NEXT: frozenset({"first", "last", "total", "path", "version", "next"}),
})
