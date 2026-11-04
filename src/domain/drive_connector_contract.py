"""The drive connector's tool contract: its tool names, each tool's parameter names, and the keys its two Firestore
tokens must carry (DRIVE_MCP_CONNECTOR_RFC §4.1, §5.2, §5.5, §6.2, §7.7).

The words live in the tokens `MCP_DRIVE_TOOL_TEXTS` and `MCP_DRIVE_RESULT_TEXTS`; this module only fixes their key
sets, which `ConnectorTexts.from_bodies` checks: a missing key is an error, an unknown one is returned apart and
logged as a warning by the loader (RFC §14.8: a token may run ahead of the revision that reads its new keys).

The tool names are the chat tools' names on purpose: result texts reused as they are name them (the passages cut line
"read_passages from passage N continues", the open bound's route), so one name serves both surfaces. The parameters
mirror the chat tools' parameters (none is dropped, since the chat's read tools take no `file_contents` item and no
specialist-only field), plus the connector's own (RFC §14.2, §14.6): `wide` on `search_drive` and `find_passages`,
`batch` on `find_passages`. With those the connector's parameters are no longer a pure mirror of the chat tools'; the
chat's specialist has its ledger and takes neither.

Result keys:
- the reading lines by the names `drive_answer.reading_keys` emits (the keys of `CHAT_READING_TEXTS`);
- one line per `Withheld` code (`W_IMAGE`, …), printed as is;
- the status and what-to-do lines, the unfinished line and the passages' language line (the chat's `STATUS_HEAD`,
  `WHAT_TO_DO`, `ANSWER_FROM_RESULTS`, `UNFINISHED_LINE`, `LANGUAGE_LINE` re-voiced);
- `ROUTE` after an open refused as too long (in place of `ROUTE_SPECIALIST`), `PAGE_NEXT` (in place of the excerpt
  renderer's `PAGE_NEXT`), `REFUSED` (the §13 refusal line) and `REFUSED_GUIDANCE` (its companion guidance, §5.2),
  `OVER_SIZE` (the line the 100,000-character guard appends, §6.1), `NOT_CONNECTED` (no drive is shared with the
  connector, or it needs reconnecting: in place of the chat's "not connected" line, §7.4), `LINK` (a one-time download
  link, §8);
- the paging lines (§14.4): `BATCH_NEXT` (a find left ranked passages out: batch k+1 with the arguments to resend),
  `BATCH_NEXT_WIDE` (its half saying `wide` returns about twice as many, after a call that was not wide), `BATCH_END`
  (a batch that selected nothing new), `SEARCH_MORE` / `SEARCH_MORE_WIDE` (a search tier showed fewer files than it
  matched; the wide form keeps only the folder half).

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
GET_DOWNLOAD_LINK = "get_download_link"        # the one-time link (RFC §8); the connector's own tool, no chat twin

TOOL_NAMES: Tuple[str, ...] = (SEARCH_DRIVE, FIND_PASSAGES, READ_PASSAGES, OPEN_DRIVE_FILE, LIST_FOLDER, GET_NOTE,
                               GET_DOWNLOAD_LINK)

TOOL_PARAMETERS: Mapping[str, Tuple[str, ...]] = MappingProxyType({
    SEARCH_DRIVE: ("query", "folder", "drives", "kind", "date_from", "date_to", "wide"),
    FIND_PASSAGES: ("question", "folder", "drives", "file_refs", "wide", "batch"),
    READ_PASSAGES: ("file_ref", "passages"),
    OPEN_DRIVE_FILE: ("file_ref",),
    LIST_FOLDER: ("folder",),
    # whole_drive_name stays: a whole-drive note is readable for a drive the connector sees (RFC §7.5)
    GET_NOTE: ("item_ref", "whole_drive_name"),
    GET_DOWNLOAD_LINK: ("file_ref",),
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
LINK = "LINK"                                  # a one-time download link; {link} is its URL (RFC §8)
NOT_CONNECTED = "NOT_CONNECTED"                # no drive shared with the connector, or one needs reconnecting
BATCH_NEXT = "BATCH_NEXT"                      # a find left ranked passages out; {batch} the next, {arguments} to resend
BATCH_NEXT_WIDE = "BATCH_NEXT_WIDE"            # after BATCH_NEXT on a call that was not wide: `wide` gives ~twice as many
BATCH_END = "BATCH_END"                        # a batch that selected nothing new: no further passages here
SEARCH_MORE = "SEARCH_MORE"                    # a search tier showed fewer than it matched: `wide`, or a group's folder
SEARCH_MORE_WIDE = "SEARCH_MORE_WIDE"          # the same after a wide search: a group's folder only

LINE_KEYS: Tuple[str, ...] = (STATUS, WHAT_TO_DO, ANSWER_FROM_RESULTS, UNFINISHED, LANGUAGE, ROUTE, PAGE_NEXT,
                              REFUSED, REFUSED_GUIDANCE, OVER_SIZE, NOT_CONNECTED, LINK)
PAGING_KEYS: Tuple[str, ...] = (BATCH_NEXT, BATCH_NEXT_WIDE, BATCH_END, SEARCH_MORE, SEARCH_MORE_WIDE)

REQUIRED_RESULT_KEYS: FrozenSet[str] = frozenset({*READING_KEYS, *WITHHELD_KEYS, *LINE_KEYS, *PAGING_KEYS})

# The same placeholder names as the chat constants these texts replace, so code formats either with one call.
RESULT_PLACEHOLDERS: Mapping[str, FrozenSet[str]] = MappingProxyType({
    STATUS: frozenset({"status"}),
    WHAT_TO_DO: frozenset({"line"}),
    ROUTE: frozenset({"ref"}),
    LINK: frozenset({"link"}),
    PAGE_NEXT: frozenset({"first", "last", "total", "path", "version", "next"}),
    BATCH_NEXT: frozenset({"batch", "arguments"}),
})
