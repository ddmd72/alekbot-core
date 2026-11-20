"""Extract a provider's contract: a small OpenAPI 3.1 subset of the operations and fields our adapters send, and of
the responses they read.

TEST_COVERAGE_SYSTEMIC_RFC §4a "Contracts", P17. The output is `contracts/providers/<provider>/openapi.yaml`, written by
this script only, never by hand; `tests/contracts/openapi_contract.py` validates our requests (`assert_request`) and the
provider's responses (`assert_response`, `assert_stream`) against it. A response (`Operation.responses`, see `Response`)
keeps the top-level fields the adapter reads and, by name with any value, the ones it does not; below the top level it
is built like a request body (variants, ALLOWED gaps, closed objects).

What a subset holds, per operation in `PROVIDERS`:
- the top-level body fields we send (`Operation.fields`); every other field is pruned, so a new field fails the
  conformance tests until it is listed here and the subset regenerated;
- at each union in `Operation.variants`, only the variants we send, chosen by their `type` const (a variant without one:
  by its JSON type, e.g. "object", "string");
- `Operation.open` paths as free-form objects: they carry our own JSON Schemas (tool parameters, output formats);
- `Operation.allowed`: what our adapter sends although the source lacks it (or marks it deprecated), each injected with
  `x-alek-extension: <reason> (<date>)`. The run refuses to write when the source now has an allowed item: the entry is
  stale and must be removed. A gap can be the value null (`Allowed.null`) and can be kept to one block type
  (`Allowed.variant`) when the evidence shows no more.
- `Operation.raw_body`: a request body that is not JSON (an upload's bytes) is kept with its media type and not validated.
- `Provider.aliases`: the subset's `x-alek-aliases`, the path rewrites `tests/contracts/openapi_contract.py` applies before
  the lookup (Microsoft Graph's `/me/drive/...` and colon addressing name operations the spec writes only as
  `/drives/{drive-id}/items/...`). Every alias target must name an operation of the subset.
Transforms (named in the file's `x-transforms`): see `_TRANSFORMS`; an OData source (`Provider.odata`) adds
`_ODATA_TRANSFORMS`, applied to its spec before anything is built.

Parameters: every path parameter of a kept operation, the query parameters in `Operation.query` and the headers in
`Operation.headers`; nothing else. An operation without `fields` sends no body (a GET).

Sources: an OpenAPI spec on GitHub pinned by commit (`Provider.repo`), a Google discovery document on GitHub pinned the
same way (`Provider.discovery`, converted to OpenAPI by `discovery_document`), or a Stainless SDK's generated request
types (`Provider.sdk`, pinned by the installed version; Anthropic publishes no reachable spec).

Usage (make contract-extract PROVIDER=openai|anthropic|gmail|graph [REF=<sha>]):
    python -m scripts.contracts.extract_subset --provider openai [--ref <sha>]   # no ref: the default branch's head
    python -m scripts.contracts.extract_subset --provider gmail [--ref <sha>]    # a discovery document, same pinning
    python -m scripts.contracts.extract_subset --provider graph [--ref <sha>]    # 45 MB of YAML: ~20 s, ~1 GB
    python -m scripts.contracts.extract_subset --provider anthropic              # the installed SDK
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import re
import subprocess
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import jsonschema
import pydantic
import typing_extensions
import yaml

from scripts.contracts.spec_drift import _discovery_methods, _pointer, fetch_url, normalize, parse

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "providers"
MAX_BYTES = 100_000


# ------------------------------------------------------------------------------------------------------- the table
@dataclass(frozen=True)
class Allowed:
    """Something our adapter sends that the source lacks or marks deprecated. Exactly one of four kinds:
    `value` (an enum literal, or the `type` of a union variant, at `path`), `null` (the JSON value null at `path`),
    `field` + `schema` (a field the object at `path` lacks; at path "query": a query parameter the operation lacks),
    `field` alone (a field of the object at `path` the source marks deprecated, which we keep sending). `variant`
    narrows a `value`, `null` or `field` + `schema` gap to the nodes whose nearest typed object (the object at `path`
    itself for a field, the object holding the node for a value) has that `type`: a path is transparent to unions, so
    without it every variant reaching the path gets the gap."""

    path: str                                  # body path ("" = the body), "header:<name>" or "query"
    reason: str                                # the fact: what we send, where, why the source lacks it
    date: str                                  # when the gap was verified, YYYY-MM-DD
    value: str | None = None
    field: str | None = None
    schema: Mapping[str, Any] | None = None
    null: bool = False
    variant: str | None = None


@dataclass(frozen=True)
class Response:
    """One response of an operation (a status and a media type) and the parts of it our adapter reads. Paths as in
    `Operation`; the root of a `text/event-stream` response is ONE event's `data` JSON (OpenAPI 3.1 has no item schema
    for a stream), so its root is the union of the event types.

    Top level: the fields in `fields` are materialised from the source like a request body; every other top-level field
    of the source is kept BY NAME with an any-value schema (`x-alek-unread`), so the object stays closed (a field the
    provider adds fails) without copying what we never read (OpenAI echoes the whole request back). Below the top level
    every kept object is materialised and closed, as on the request side."""

    fields: tuple[str, ...]                             # top-level fields the adapter reads (materialised)
    status: str = "200"
    media: str = "application/json"
    variants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # union path -> variants kept
    open: tuple[str, ...] = ()                          # free-form paths
    allowed: tuple[Allowed, ...] = ()                   # what the provider sends although the source lacks it
    sdk_model: str = ""                                 # SDK source: the response type, "module:Name"
    reads: str = ""                                     # where the adapter reads it (file:lines), for the reader


@dataclass(frozen=True)
class Operation:
    """One operation and the parts of it we send. Paths: `a.b` is a property, `a[]` an array's items; unions are
    transparent (`input[].content[]` reaches the content list of every kept `input` variant)."""

    method: str                                         # "post"
    path: str                                           # as in the source; an SDK source names the REST path
    fields: tuple[str, ...]                             # the top-level body fields we send; () = no body
    variants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # union path -> variants kept
    open: tuple[str, ...] = ()                          # free-form paths (our own JSON Schemas)
    allowed: tuple[Allowed, ...] = ()
    sdk_body: str = ""                                  # SDK source: the request TypedDict, "module:Name"
    headers: Mapping[str, str] = field(default_factory=dict)        # SDK source: header -> Literal type "module:Name"
    header_aliases: Mapping[str, str] = field(default_factory=dict)  # SDK source: body field -> its header alias
    query: tuple[str, ...] = ()                         # the query parameters we send (path parameters: all kept)
    raw_body: str = ""                                  # a non-JSON body's media type (kept, never validated)
    responses: tuple[Response, ...] = ()                # the responses our adapter reads


@dataclass(frozen=True)
class Alias:
    """A path rewrite applied before the operation lookup: a request path matching `pattern` is looked up as `target`.
    Both are written like contract paths: `{name}` is one path segment's text (or part of one); a trailing `/**` is
    the rest of the path, carried over. A `{name}` of the target the pattern does not capture gets a placeholder
    value. Rules apply in order, the first match wins (`tests/contracts/openapi_contract.py` applies them)."""

    pattern: str
    target: str
    reason: str


@dataclass(frozen=True)
class Provider:
    """A provider's request contract. Exactly one source: `repo` + `spec_file` (OpenAPI on GitHub, or a Google
    discovery document when `discovery`) or `sdk`."""

    name: str
    operations: tuple[Operation, ...]
    repo: str = ""            # "openai/openai-openapi"
    spec_file: str = ""       # "openapi.yaml"
    discovery: bool = False   # `spec_file` is a Google discovery document: its kept methods become OpenAPI operations
    sdk: str = ""             # "anthropic": the installed package and its version
    server: str = ""          # the base URL operation paths are relative to (an SDK source has no `servers`)
    odata: bool = False       # an OData-generated spec (Microsoft Graph): `_ODATA_TRANSFORMS` apply first
    aliases: tuple[Alias, ...] = ()


_OPENAI_ADAPTER = "src/adapters/openai_adapter.py"
_CLAUDE_ADAPTER = "src/adapters/claude_adapter.py"
_RECORDINGS = "tests/fixtures/providers"
# The content blocks a Claude response carries that the adapter reads or passes back: text (with citations), tool_use,
# thinking and redacted_thinking (replayed in raw_content), and the server tools' blocks of the grounding path (web
# search and fetch, and the code execution the dynamic search tools bring). Not kept: tool search, container upload.
_CLAUDE_BLOCKS = (
    "text", "thinking", "redacted_thinking", "tool_use", "server_tool_use", "web_search_tool_result",
    "web_fetch_tool_result", "code_execution_tool_result", "bash_code_execution_tool_result",
    "text_editor_code_execution_tool_result",
)
_GMAIL_PATH = "/gmail/v1/users/{userId}/messages"
_TODO_LISTS = "/me/todo/lists"
_TODO_LIST = _TODO_LISTS + "/{todoTaskList-id}"
_TODO_TASKS = _TODO_LIST + "/tasks"
_TODO_TASK = _TODO_TASKS + "/{todoTask-id}"
_TODO_CHECKLIST = _TODO_TASK + "/checklistItems"
_DRIVE_ITEM = "/drives/{drive-id}/items/{driveItem-id}"
_ONEDRIVE_ADAPTER = "src/adapters/microsoft/onedrive_adapter.py"
_DRIVEITEM_DOC = "https://learn.microsoft.com/en-us/graph/api/resources/driveitem"
# The instance annotation @microsoft.graph.conflictBehavior: documented, absent from the OData-generated spec everywhere
_CONFLICT = {"type": "string", "enum": ["fail", "rename", "replace"]}
_CONFLICT_QUERY_REASON = (
    f"the conflict behaviour of a simple upload by name, a URL parameter documented at {_DRIVEITEM_DOC} (instance "
    f"attributes; microsoft-graph-docs-contrib api-reference/v1.0/resources/driveitem.md:148,153), absent from the spec "
    f"({_ONEDRIVE_ADAPTER} upload)")
_CONFLICT_BODY_REASON = (
    "the instance annotation of a new folder, documented at https://learn.microsoft.com/en-us/graph/api/"
    "driveitem-post-children (microsoft-graph-docs-contrib driveitem-post-children.md:59,72), absent from the spec "
    f"({_ONEDRIVE_ADAPTER} create_folder)")
_CONFLICT_SESSION_REASON = (
    "the instance annotation of an upload session's item, documented at https://learn.microsoft.com/en-us/graph/api/"
    "driveitem-createuploadsession (microsoft-graph-docs-contrib driveitem-createuploadsession.md:80), absent from "
    f"the spec ({_ONEDRIVE_ADAPTER} upload, replace_content)")
# The documented sizes: small, medium, large and the custom c{width}x{height} with an optional _crop
_THUMBNAIL_SIZE = {"type": "string", "pattern": "^(small|medium|large|c[1-9][0-9]*x[1-9][0-9]*(_crop)?)$"}
_THUMBNAIL_SELECT_REASON = (
    "the unprefixed select naming one thumbnail size (/thumbnails?select=c300x400_crop), documented at "
    "https://learn.microsoft.com/en-us/graph/api/driveitem-list-thumbnails (microsoft-graph-docs-contrib "
    f"driveitem-list-thumbnails.md:121,376); the spec declares only $select ({_ONEDRIVE_ADAPTER} get_thumbnail)")

PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        name="openai", repo="openai/openai-openapi", spec_file="openapi.yaml",
        operations=(Operation(
            # OpenAIAdapter.generate_content: client.responses.create (body built at :341-411)
            method="post", path="/responses",
            fields=("input", "instructions", "max_output_tokens", "model", "prompt_cache_key",
                    "prompt_cache_retention", "reasoning", "store", "text", "tool_choice", "tools"),
            variants={
                # user/assistant/developer messages, function calls and their outputs, and the output items of a
                # previous response passed back as they came (raw_content: message, reasoning, web_search_call)
                "input[]": ("message", "function_call", "function_call_output", "reasoning", "web_search_call"),
                "input[].content[]": ("input_text", "input_image", "input_file", "output_text", "reasoning_text"),
                "tools[]": ("function", "web_search"),
                "tool_choice": ("string",),              # "auto" | "required"
                "reasoning": ("object",),
                "text.format": ("json_schema", "json_object"),
            },
            open=("tools[].parameters", "text.format.schema"),
            allowed=(Allowed(
                path="", field="prompt_cache_retention", date="2026-11-18",
                reason=f"deprecated in the spec; sent only for models before GPT-5.6, where it still sets the 24h "
                       f"retention ({_OPENAI_ADAPTER} _DEPRECATED_RETENTION_PREFIXES, generate_content)",
            ),),
            responses=(Response(
                # Not streamed: one JSON body. _parse_response reads output (message output_text + url_citation
                # annotations, function_call name/arguments/call_id, web_search_call action) and usage (input, cached
                # and cache-write input, output); _map_finish_reason reads status and incomplete_details.reason; model
                # is logged. The output items are passed back as the next request's input (raw_content).
                reads=f"{_OPENAI_ADAPTER}:735-885 (_map_finish_reason, _parse_response)",
                fields=("incomplete_details", "model", "output", "status", "usage"),
                variants={
                    "output[]": ("message", "function_call", "reasoning", "web_search_call"),
                    "output[].content[]": ("output_text", "reasoning_text"),
                },
                allowed=tuple(Allowed(
                    path="", field=name, schema=schema, date="2026-11-19",
                    reason=f"every recorded response carries {name} ({_RECORDINGS}/openai/*.json); the spec's "
                           f"Response lacks it; not read by the adapter",
                ) for name, schema in (
                    ("billing", {"type": "object", "properties": {"payer": {"type": "string"}},
                                 "required": ["payer"], "additionalProperties": False}),
                    ("frequency_penalty", {"type": "number"}),
                    ("presence_penalty", {"type": "number"}),
                    ("store", {"type": "boolean"}),
                )),
            ),),
        ),),
    ),
    "anthropic": Provider(
        name="anthropic", sdk="anthropic", server="https://api.anthropic.com",
        operations=(Operation(
            # ClaudeAdapter.generate_content and _grounded_stream_loop: client.messages.stream
            method="post", path="/v1/messages",
            sdk_body="anthropic.types.message_create_params:MessageCreateParamsStreaming",
            fields=("container", "max_tokens", "messages", "model", "output_config", "stream", "system",
                    "thinking", "tool_choice", "tools"),
            variants={
                # our own blocks, and an assistant turn passed back as it came (raw_content; the grounded loop's
                # pause_turn continuation carries the server tools' blocks)
                "messages[].content[]": (
                    "text", "image", "document", "tool_use", "tool_result", "thinking", "redacted_thinking",
                    "server_tool_use", "web_search_tool_result", "web_fetch_tool_result",
                    "code_execution_tool_result", "bash_code_execution_tool_result",
                    "text_editor_code_execution_tool_result",
                ),
                "tools[]": ("custom", "web_search_20260209", "web_fetch_20260209", "web_search_20250305",
                            "web_fetch_20250910"),
                "thinking": ("adaptive", "disabled"),
                "tool_choice": ("auto", "any"),
                "output_config.format": ("json_schema",),
                "system[]": ("text",),
            },
            open=("tools[].input_schema", "output_config.format.schema"),
            headers={"anthropic-beta": "anthropic.types.anthropic_beta_param:AnthropicBetaParam"},
            header_aliases={"user_profile_id": "anthropic-user-profile-id"},
            allowed=(
                Allowed(
                    path="thinking", value="between_tools", date="2026-11-18",
                    reason=f"Sonnet 5.5 rejects thinking.type=disabled; between_tools is its no-thinking floor "
                           f"({_CLAUDE_ADAPTER} _THINKING_OFF), absent from the SDK's ThinkingConfigParam",
                ),
                Allowed(
                    path="header:anthropic-beta", value="web-search-2025-03-05", date="2026-11-18",
                    reason=f"sent with the legacy web_search_20250305 tool on the grounding path of models without "
                           f"dynamic search ({_CLAUDE_ADAPTER} generate_content beta_headers), absent from the SDK's "
                           f"AnthropicBetaParam literals",
                ),
                Allowed(
                    path="messages[].content[].caller", null=True, variant="server_tool_use", date="2026-11-19",
                    reason=f"P17-D1: a server_tool_use block replayed as raw_content carries caller: null (the SDK's "
                           f"stream accumulator sets it when the block's start event has none); the API answered 200 "
                           f"to it ({_RECORDINGS}/anthropic/replay_server_tool_blocks.json, second request); the "
                           f"SDK's ServerToolUseBlockParam has no null caller",
                ),
            ),
            responses=(
                Response(
                    # The message the SDK assembles from the stream (stream.get_final_message): _parse_response
                    # reads content (text, tool_use; thinking skipped), usage (input, output, cache read and creation)
                    # and stop_reason; _raise_on_refusal reads stop_details; model is logged. The grounded loop sums
                    # usage per call, branches on stop_reason and passes the content back on pause_turn; raw_content
                    # replays the whole content list on the next turn.
                    sdk_model="anthropic.types:Message",
                    reads=f"{_CLAUDE_ADAPTER}:627-639, 709-742, 1277-1387 (_raise_on_refusal, _grounded_stream_loop, "
                          f"_map_finish_reason, _parse_response)",
                    fields=("content", "model", "stop_details", "stop_reason", "usage"),
                    variants={"content[]": _CLAUDE_BLOCKS},
                ),
                Response(
                    # What is on the wire: we always stream. One event's data; the SDK accumulates the events into
                    # the Message above, and the grounded loop reads message_delta.delta.container.id itself.
                    media="text/event-stream", sdk_model="anthropic.types:RawMessageStreamEvent",
                    reads=f"{_CLAUDE_ADAPTER}:495-499, 678-687 (client.messages.stream, the container id of "
                          f"message_delta)",
                    fields=("content_block", "delta", "index", "message", "type", "usage"),
                    variants={
                        "": ("message_start", "message_delta", "message_stop", "content_block_start",
                             "content_block_delta", "content_block_stop"),
                        "message.content[]": _CLAUDE_BLOCKS,
                        "content_block": _CLAUDE_BLOCKS,
                    },
                    allowed=(
                        Allowed(
                            path="", value="ping", date="2026-11-19",
                            reason="Anthropic's streaming documentation: a stream may carry any number of ping events "
                                   "(data {\"type\": \"ping\"}); the SDK passes on only the event names it knows "
                                   "(anthropic/_streaming.py Stream.__stream__) and drops ping, so its event types "
                                   "lack it",
                        ),
                        Allowed(
                            path="message", field="diagnostics", schema={"type": "null"}, date="2026-11-19",
                            reason=f"message_start's message carries diagnostics (null) in every recorded stream "
                                   f"({_RECORDINGS}/anthropic/*.json); the SDK's Message lacks it",
                        ),
                        Allowed(
                            path="message.usage", field="speed", schema={"type": "string"}, date="2026-11-19",
                            reason=f"message_start's usage carries speed (a string, \"standard\") in every recorded "
                                   f"stream ({_RECORDINGS}/anthropic/*.json); the SDK's Usage lacks it",
                        ),
                        Allowed(
                            path="content_block.content", field="abort_reason", schema={"type": "null"},
                            variant="code_execution_result", date="2026-11-19",
                            reason=f"a code_execution_tool_result block's code_execution_result carries abort_reason "
                                   f"(null) ({_RECORDINGS}/anthropic/grounding_dynamic_container.json); the SDK's "
                                   f"CodeExecutionResultBlock lacks it",
                        ),
                    ),
                ),
            ),
        ),),
    ),
    "gmail": Provider(
        name="gmail", repo="googleapis/discovery-artifact-manager", spec_file="discoveries/gmail.v1.json",
        discovery=True,
        operations=(
            # gmail.users.messages.list: GmailProviderAdapter.list_emails, list_headers
            Operation(method="get", path=_GMAIL_PATH, fields=(), query=("maxResults", "pageToken", "q")),
            # gmail.users.messages.get: _fetch_meta (format=metadata + metadataHeaders), batch_get_full_content and
            # _fetch_message (format=full)
            Operation(method="get", path=_GMAIL_PATH + "/{id}", fields=(), query=("format", "metadataHeaders")),
            # gmail.users.messages.attachments.get: get_part with an attachment handle
            Operation(method="get", path=_GMAIL_PATH + "/{messageId}/attachments/{id}", fields=()),
        ),
    ),
    "graph": Provider(
        name="graph", repo="microsoftgraph/msgraph-metadata", spec_file="openapi/v1.0/openapi.yaml", odata=True,
        operations=(
            # MicrosoftToDoAdapter (src/adapters/microsoft/todo_adapter.py)
            Operation(method="get", path=_TODO_LISTS, fields=()),                         # list_task_lists
            Operation(method="post", path=_TODO_LISTS, fields=("displayName",)),          # ensure_primary_list
            Operation(method="get", path=_TODO_LIST, fields=()),                          # the list's name
            Operation(method="get", path=_TODO_TASKS, fields=(), query=("$filter",)),     # list_tasks
            Operation(method="post", path=_TODO_TASKS, fields=(                           # create_task
                "body", "categories", "checklistItems", "dueDateTime", "importance", "isReminderOn",
                "linkedResources", "recurrence", "reminderDateTime", "startDateTime", "title")),
            Operation(method="get", path=_TODO_TASK, fields=()),                          # get_task
            Operation(method="patch", path=_TODO_TASK, fields=(                           # update_task
                "body", "categories", "dueDateTime", "importance", "isReminderOn", "recurrence", "reminderDateTime",
                "startDateTime", "status", "title")),
            Operation(method="delete", path=_TODO_TASK, fields=()),                       # delete_task
            Operation(method="get", path=_TODO_CHECKLIST, fields=()),                     # _sync_checklist
            Operation(method="post", path=_TODO_CHECKLIST, fields=("displayName", "isChecked")),
            Operation(method="patch", path=_TODO_CHECKLIST + "/{checklistItem-id}", fields=("displayName", "isChecked")),
            Operation(method="delete", path=_TODO_CHECKLIST + "/{checklistItem-id}", fields=()),
            Operation(method="post", path="/subscriptions", fields=(                      # register_subscription
                "changeType", "clientState", "expirationDateTime", "notificationUrl", "resource")),
            Operation(method="patch", path="/subscriptions/{subscription-id}", fields=("expirationDateTime",)),
            Operation(method="delete", path="/subscriptions/{subscription-id}", fields=()),
            # MicrosoftOAuthProvider.exchange (src/adapters/microsoft/microsoft_oauth_provider.py): the account identity
            Operation(method="get", path="/me", fields=()),
            # OneDriveAdapter (src/adapters/microsoft/onedrive_adapter.py); the adapter's /me/drive/... paths reach
            # these through the aliases below
            Operation(method="get", path="/drives/{drive-id}/special/{driveItem-id}", fields=()),   # the App Folder
            Operation(method="get", path=_DRIVE_ITEM, fields=()),                         # get_item, download, ...
            Operation(method="patch", path=_DRIVE_ITEM, fields=("name", "parentReference")),         # move
            Operation(method="delete", path=_DRIVE_ITEM, fields=()),                      # delete
            Operation(method="get", path=_DRIVE_ITEM + "/children", fields=(), query=("$top",)),     # list_children
            Operation(method="post", path=_DRIVE_ITEM + "/children",                      # create_folder
                      fields=("@microsoft.graph.conflictBehavior", "folder", "name"),
                      allowed=(Allowed(path="", field="@microsoft.graph.conflictBehavior", schema=_CONFLICT,
                                       date="2026-11-19", reason=_CONFLICT_BODY_REASON),)),
            Operation(method="get", path=_DRIVE_ITEM + "/thumbnails", fields=(),          # get_thumbnail
                      allowed=(Allowed(path="query", field="select", schema=_THUMBNAIL_SIZE, date="2026-11-19",
                                       reason=_THUMBNAIL_SELECT_REASON),)),
            Operation(method="put", path=_DRIVE_ITEM + "/content", fields=(),             # upload, replace_content
                      raw_body="application/octet-stream",
                      allowed=(Allowed(path="query", field="@microsoft.graph.conflictBehavior", schema=_CONFLICT,
                                       date="2026-11-19", reason=_CONFLICT_QUERY_REASON),)),
            Operation(method="post", path=_DRIVE_ITEM + "/createUploadSession", fields=("item",),   # large uploads
                      allowed=(Allowed(path="item", field="@microsoft.graph.conflictBehavior", schema=_CONFLICT,
                                       date="2026-11-19", reason=_CONFLICT_SESSION_REASON),)),
            Operation(method="get", path=_DRIVE_ITEM + "/delta()", fields=(), query=("$top",)),     # get_changes
            Operation(method="get", path=_DRIVE_ITEM + "/search(q='{q}')", fields=(), query=("$top",)),   # search
        ),
        aliases=(
            Alias("/me/drive/items/{driveItem-id}:/{path}:/**", _DRIVE_ITEM + "/**",
                  "colon addressing: the item at <path> under item <driveItem-id> is an item, so an operation on it "
                  "(/content, /createUploadSession) is the item's operation"),
            Alias("/me/drive/items/{driveItem-id}:/{path}:", _DRIVE_ITEM,
                  "colon addressing: the item at <path> under item <driveItem-id> is an item"),
            Alias("/me/drive/special/approot/delta", _DRIVE_ITEM + "/delta()",
                  "the App Folder (the one special folder the adapter uses) is an item; the adapter writes the "
                  "function without parentheses, which Graph accepts, the spec writes delta()"),
            Alias("/me/drive/special/approot/search(q='{q}')", _DRIVE_ITEM + "/search(q='{q}')",
                  "the App Folder (the one special folder the adapter uses) is an item"),
            Alias("/me/drive/special/approot", "/drives/{drive-id}/special/{driveItem-id}",
                  "/me/drive is the signed-in user's drive; approot is the one special folder the adapter uses"),
            Alias("/me/drive/items/**", "/drives/{drive-id}/items/**", "/me/drive is the signed-in user's drive"),
        ),
    ),
}

_TRANSFORMS = (
    "prune: only the listed top-level fields and union variants are kept",
    "flatten-allOf: an allOf of objects becomes one object (properties and required merged)",
    "oneOf-to-anyOf: variants overlap (a message with list content matches two of them)",
    "close-objects: additionalProperties false on every object that declares properties, except the open paths",
    "open-paths: our own JSON Schemas (the open paths) are free-form objects",
    "drop-redundant-literals: a literal next to a plain {type: T} in a union accepts nothing more (model id lists)",
    "drop-doc-keys: documentation keys and vendor doc extensions dropped (spec_drift.normalize); property names kept",
    "stringify-dates: YAML dates become ISO strings",
)
_SDK_TRANSFORMS = (
    "sdk-schema: pydantic TypeAdapter(<request TypedDict>).json_schema(), $defs as components",
    "drop-header-aliases: fields the SDK sends as headers (PropertyInfo alias) are not body fields",
    "drop-sdk-response-models: union members that are SDK response models (accepted as input for convenience) are "
    "dropped; the request types (*Param) are the wire contract",
    "header-literals: a header's allowed values are its SDK type's Literal values",
)
_RESPONSE_TRANSFORMS = (
    "response-unread: a top-level response field the adapter does not read is kept by name with an any-value schema "
    "(x-alek-unread); the object stays closed",
    "response-stream-item: a text/event-stream schema is one event's data JSON (OpenAPI 3.1 has no item schema)",
)
_SDK_RESPONSE_TRANSFORMS = (
    "sdk-response-schema: pydantic TypeAdapter(<response model>).json_schema(), $defs as components",
    "sdk-response-closed: the SDK's response models accept extra fields (additionalProperties true); the subset "
    "closes every one that declares properties",
)
UNREAD = "x-alek-unread"
_UNREAD_NOTE = "not read by our adapter: any value"
_DISCOVERY_TRANSFORMS = (
    "discovery-to-openapi: each kept method is an operation at /<path> (operationId = the method id) under the server "
    "rootUrl + servicePath; the API-wide parameters apply to every method",
    "discovery-parameters: location path|query becomes in path|query (a path parameter is required); repeated becomes "
    "an array of the type; string-typed default, minimum and maximum take the parameter's type; an integer format "
    "(int32, uint32, int64, uint64) becomes its range as minimum and maximum unless the method states them; "
    "deprecated is kept",
)
_PARAMETER_TRANSFORM = (
    "prune-parameters: every path parameter, only the listed query parameters and headers; $refs inlined, "
    "documentation keys dropped"
)
_ODATA_TRANSFORMS = (
    "odata-strip: discriminator and x-ms-* keys dropped from the source before anything is built (entity's "
    "discriminator maps every type; the x-ms-* keys are generator annotations)",
    "odata-type-dropped: the @odata.type property and its required entry dropped from every schema (every complex "
    "type requires it; a client sends it only to name a derived type, and we never do: sending it fails as unknown)",
    "odata-integers: {type: number, format: int16|int32|int64} becomes {type: integer} (bounds kept): OData's whole "
    "numbers, which a fraction must fail",
    "odata-key-segments: a key path parameter (drive-id, *-id) takes no ':' (pattern ^[^:]+$): a colon in a segment "
    "is OData's path addressing, never part of a key",
    "odata-string-literals: a path parameter inside an OData string literal ('{name}') has every quote doubled "
    "(pattern ^([^']|'')*$)",
    "nullable-to-3.1: OpenAPI 3.0 nullable becomes JSON Schema 2020-12: {type: T, nullable: true} -> {type: [T, null]} "
    "(null added to an enum), and the null member of a nullable reference, anyOf [{$ref}, {type: object, nullable: "
    "true}], becomes {type: null}",
)
_RAW_BODY_TRANSFORM = (
    "raw-body: a request body that is not JSON (Operation.raw_body, e.g. application/octet-stream) is kept with its "
    "media type and schema as declared; its bytes are not validated"
)
_ALIAS_TRANSFORM = (
    "aliases: x-alek-aliases rewrites a request path (in order, first match) to the contract path it is looked up "
    "under; every target names a kept operation"
)


# ------------------------------------------------------------------------------------------------------ the subset
_REF_PREFIX = "#/components/schemas/"
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_ONE = ("items", "not", "contains", "if", "then", "else", "propertyNames")
_DROPPED = frozenset({"discriminator", "$schema", "$id"})


def _plain(value: Any) -> Any:
    """YAML dates as ISO strings, recursively."""
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _join(path: str, step: str) -> str:
    if step == "[]":
        return path + "[]"
    return f"{path}.{step}" if path else step


class _Registry:
    """The components of one subset, shared by every body built into it: the same source schema built the same way is
    one component; built differently (other variants kept) it gets a numbered name."""

    def __init__(self) -> None:
        self.components: dict[str, Any] = {}
        self.names: dict[tuple[str, str], str] = {}


class _Builder:
    """Materialises one body schema (an operation's request, or one of its responses) from a source spec:
    path-dependent pruning, refs kept as components."""

    def __init__(self, spec: dict, op: "Operation | Response", *, registry: _Registry | None = None,
                 response: bool = False):
        self.spec = spec
        self.op = op
        self.response = response
        registry = registry or _Registry()
        self.components = registry.components
        self._names = registry.names
        self._stack: list[str] = []
        self.reached: set[str] = set()
        self._found: dict[str, set[str]] = {}
        self._root_fields: set[str] = set()
        self._allowed = {a.path: [] for a in op.allowed}
        for a in op.allowed:
            self._allowed[a.path].append(a)
        self._typed: list[set[str]] = []        # the `type` values of the typed objects being built, innermost last
        self._narrowed: set[int] = set()         # the variant-narrowed ALLOWED entries applied somewhere

    # -- source navigation
    def resolve(self, node: Any) -> Any:
        seen = 0
        while isinstance(node, dict) and "$ref" in node:
            node = _pointer(self.spec, node["$ref"])
            seen += 1
            if seen > 50:
                raise ValueError("a $ref chain does not end")
        return node

    def merged(self, node: Any) -> dict:
        """The resolved node with an allOf of objects flattened into one object (recursively)."""
        node = self.resolve(node)
        if not isinstance(node, dict) or "allOf" not in node:
            return node
        out = {k: v for k, v in node.items() if k != "allOf"}
        props: dict[str, Any] = dict(out.get("properties") or {})
        required: list[str] = list(out.get("required") or [])
        for member in node["allOf"]:
            part = self.merged(member)
            if not isinstance(part, dict) or not (part.get("type") == "object" or "properties" in part):
                raise ValueError(f"allOf member is not an object: {_canon(part)[:200]}")
            if part.get("anyOf") or part.get("oneOf"):
                raise ValueError("allOf member with its own union: not flattenable")
            props.update(part.get("properties") or {})
            required += [r for r in part.get("required") or [] if r not in required]
            for key, value in part.items():
                if key not in ("properties", "required", "type") and key not in out:
                    out[key] = value
        out["type"] = "object"
        out["properties"] = props
        if required:
            out["required"] = required
        return out

    def members(self, node: Any) -> list[Any]:
        """A union's leaf members (nested unions flattened); each member as written (a $ref stays a $ref)."""
        resolved = self.merged(node)
        union = resolved.get("anyOf") or resolved.get("oneOf") if isinstance(resolved, dict) else None
        if not union:
            return [node]
        if "properties" in resolved:
            raise ValueError(f"a union next to properties: {_canon(resolved)[:200]}")
        out: list[Any] = []
        for member in union:
            out += self.members(member)
        return out

    def type_values(self, node: Any) -> set[str] | None:
        """The `type` property's const/enum values of an object schema, or None when it has no such tag."""
        resolved = self.merged(node)
        if not isinstance(resolved, dict):
            return None
        tag = (resolved.get("properties") or {}).get("type")
        if tag is None:
            return None
        values: set[str] = set()
        for part in self.members(tag):
            part = self.resolve(part)
            if "const" in part:
                values.add(part["const"])
            values.update(v for v in part.get("enum") or [] if isinstance(v, str))
        return values or None

    def selector(self, node: Any) -> set[str]:
        tags = self.type_values(node)
        if tags:
            return tags
        resolved = self.merged(node)
        kind = resolved.get("type") if isinstance(resolved, dict) else None
        if kind is None and isinstance(resolved, dict) and "properties" in resolved:
            kind = "object"
        return set(kind) if isinstance(kind, list) else {kind} if kind else set()

    # -- materialisation
    def body(self, node: Any) -> Any:
        self._check_paths()
        root = self.build(node, "")
        body_allowed = (a.path for a in self.op.allowed if _is_body_path(a.path))
        missing = sorted({*self.op.variants, *self.op.open, *body_allowed} - self.reached)
        if missing:
            raise ValueError(f"paths never reached (typo, or the source moved them): {missing}")
        for path, wanted_variants in self.op.variants.items():
            absent = sorted(set(wanted_variants) - self._found.get(path, set()))
            if absent:
                raise ValueError(f"variants absent at {path or '<body>'}: {absent}")
        unapplied = [a.path for a in self.op.allowed if a.variant is not None and id(a) not in self._narrowed]
        if unapplied:
            raise ValueError(f"ALLOWED narrowed to a variant never applied (typo, or the source moved it): {unapplied}")
        if self.response:
            absent_fields = sorted(set(self.op.fields) - self._root_fields)
            if absent_fields:
                raise ValueError(f"listed fields absent from the source: {absent_fields}")
        return root

    def _check_paths(self) -> None:
        for path in [*self.op.variants, *self.op.open, *(a.path for a in self.op.allowed)]:
            if path == "query":
                continue
            if path.startswith("header:"):
                if path[len("header:"):] not in getattr(self.op, "headers", {}):
                    raise ValueError(f"allowed header {path!r} is not a declared header")
                continue
            if any(a.null for a in self._allowed.get(path, [])) and path in self.op.variants:
                raise ValueError(f"ALLOWED null at the union {path!r}: name a path below it")
            head = path.split(".")[0].split("[")[0]
            if path and head not in self.op.fields:
                raise ValueError(f"path {path!r} is under no listed field")

    def build(self, node: Any, path: str, *, whole: bool = True) -> Any:
        """The materialised schema at `path`. `whole` is False for a member of the union at `path` (or a resolved
        $ref): the variant selection and an ALLOWED value apply once, to the whole node at the path."""
        self.reached.add(path)
        if path in self.op.open:
            built: Any = {"type": "object"}
        elif whole and path in self.op.variants:
            built = self._variants(node, path)
        elif isinstance(node, dict) and "$ref" in node:
            built = self._component(node["$ref"], path)
        else:
            built = self._schema(self.merged(node), path)
        return self._inject_values(built, path) if whole else built

    def _component(self, ref: str, path: str) -> Any:
        name = ref.rsplit("/", 1)[-1]
        if name in self._stack:
            raise ValueError(f"recursive schema {name} at {path or '<body>'}: prune it or declare the path open")
        self._stack.append(name)
        try:
            built = self.build(self.resolve({"$ref": ref}), path, whole=False)
        finally:
            self._stack.pop()
        key = (name, _canon(built))
        if key not in self._names:
            cname, n = name, 1
            while cname in self.components:
                n += 1
                cname = f"{name}_{n}"
            self._names[key] = cname
            self.components[cname] = built
        return {"$ref": _REF_PREFIX + self._names[key]}

    def _variants(self, node: Any, path: str) -> Any:
        wanted = self.op.variants[path]
        members = self.members(node)
        by_name = [(self.selector(m), m) for m in members]
        available = set().union(*(names for names, _ in by_name)) if by_name else set()
        kept = [m for names, m in by_name if names & set(wanted)]
        injected = [a for a in self._allowed.get(path, []) if a.value is not None]
        for a in injected:
            if a.value in available:
                raise ValueError(f"stale ALLOWED at {path}: the source now has variant {a.value!r}; remove the entry")
        # A path can reach several unions (the content lists of several message variants): each keeps what it has
        # of the wanted variants, and every wanted variant must exist at one of them at least (checked in `body`).
        self._found.setdefault(path, set()).update(available & set(wanted))
        if not kept and not injected:
            raise ValueError(f"no wanted variant at a union of {path} (it has {sorted(available)})")
        built = [self.build(m, path, whole=False) for m in kept]
        built += [{
            "type": "object", "properties": {"type": {"const": a.value}}, "required": ["type"],
            "additionalProperties": False, "x-alek-extension": _note(a),
        } for a in injected]
        return built[0] if len(built) == 1 else {"anyOf": built}

    def _schema(self, node: Any, path: str) -> Any:
        if not isinstance(node, dict):
            return _plain(node)
        out: dict[str, Any] = {}
        for key in sorted(node):
            value = node[key]
            if key in _DROPPED:
                continue
            if key == "properties":
                tags = self.type_values(node)
                if tags:
                    self._typed.append(tags)
                try:
                    out[key] = self._properties(value, path)
                finally:
                    if tags:
                        self._typed.pop()
            elif key in ("anyOf", "oneOf"):
                out["anyOf"] = [self.build(m, path, whole=False) for m in value]
            elif key in _SCHEMA_LISTS:
                raise ValueError(f"{key} at {path or '<body>'}: not flattened")
            elif key == "items":
                out[key] = self.build(value, _join(path, "[]"))
            elif key in _SCHEMA_ONE or key == "additionalProperties" and isinstance(value, dict):
                out[key] = self.build(value, _join(path, "*"))
            else:
                kept = normalize({key: _plain(value)})
                out.update(kept)
        if "properties" in out:
            if any("properties" in self.merged(m) for m in node.get("anyOf") or node.get("oneOf") or []):
                raise ValueError(f"object with a union of objects at {path or '<body>'}: cannot be closed")
            out.setdefault("additionalProperties", False)
        if path == "" and not self.response:
            self._check_root_required(out)
        if "anyOf" in out:
            out["anyOf"] = self._without_redundant_literals(out["anyOf"])
            if len(out["anyOf"]) == 1 and len(out) == 1:
                return out["anyOf"][0]
        return out

    def _lookup(self, node: Any) -> Any:
        while isinstance(node, dict) and "$ref" in node:
            node = self.components[node["$ref"].rsplit("/", 1)[-1]]
        return node

    def _without_redundant_literals(self, members: list[Any]) -> list[Any]:
        """Drop a literal member (`enum`/`const` of type T) when a plain `{type: T}` is in the same union: it accepts
        nothing more (the providers' model id lists, kilobytes of names next to "any string")."""
        plain = {r["type"] for r in map(self._lookup, members)
                 if isinstance(r, dict) and set(r) == {"type"} and isinstance(r["type"], str)}

        def redundant(member: Any) -> bool:
            r = self._lookup(member)
            keys = {k for k in r if not k.startswith("x-")} if isinstance(r, dict) else set()
            return (isinstance(r, dict) and r.get("type") in plain and bool(keys & {"enum", "const"})
                    and keys <= {"type", "enum", "const", "default"})

        return [m for m in members if not redundant(m)]

    def _properties(self, props: dict, path: str) -> dict:
        names = sorted(props)
        if path == "" and self.response:
            # Every source field by name: the ones we read materialised, the others any-value (`x-alek-unread`).
            # A response root can be a union (stream events): a listed field must be in one of its objects at least.
            self._root_fields.update(n for n in names if n in self.op.fields)
            out = {name: self.build(props[name], name) if name in self.op.fields else {UNREAD: _UNREAD_NOTE}
                   for name in names}
            for a in self._applicable(""):
                if a.field is None:
                    continue
                if a.field in props:
                    raise ValueError(f"stale ALLOWED at <body>: the source now has field {a.field!r}")
                out[a.field] = {**_plain(dict(a.schema or {})), "x-alek-extension": _note(a)}
            return dict(sorted(out.items()))
        if path == "":
            absent = [f for f in self.op.fields if f not in props
                      and not any(a.path == "" and a.field == f and a.schema is not None for a in self.op.allowed)]
            if absent:
                raise ValueError(f"listed fields absent from the source: {absent}")
            names = [n for n in names if n in self.op.fields]
        out = {name: self.build(props[name], _join(path, name)) for name in names}
        for a in self._applicable(path):
            if a.field is None:
                continue
            if a.schema is not None:
                if a.field in props:
                    raise ValueError(f"stale ALLOWED at {path or '<body>'}: the source now has field {a.field!r}")
                out[a.field] = {**_plain(dict(a.schema)), "x-alek-extension": _note(a)}
            else:
                raw = props.get(a.field)
                if raw is None:
                    raise ValueError(f"ALLOWED deprecated field {a.field!r} is absent from the source")
                if not (isinstance(raw, dict) and raw.get("deprecated")):
                    raise ValueError(f"stale ALLOWED at {path or '<body>'}: {a.field!r} is no longer deprecated")
                built = out[a.field]
                if "$ref" in built:
                    raise ValueError(f"deprecated field {a.field!r} is a shared component: unsupported")
                built["x-alek-extension"] = _note(a)
        return dict(sorted(out.items()))

    def _applicable(self, path: str) -> list[Allowed]:
        """The ALLOWED entries at `path` that apply to the node being built: a `variant` one only inside (or on) the
        typed object of that type; each such application is noted for `body`'s never-applied check."""
        out = []
        for a in self._allowed.get(path, []):
            if a.variant is not None:
                if not self._typed or a.variant not in self._typed[-1]:
                    continue
                self._narrowed.add(id(a))
            out.append(a)
        return out

    def _check_root_required(self, out: dict) -> None:
        dropped = [r for r in out.get("required") or [] if r not in self.op.fields]
        if dropped:
            raise ValueError(f"required body fields not listed: {dropped}")

    def _inject_values(self, built: Any, path: str) -> Any:
        """A scalar ALLOWED value: the node at `path` becomes anyOf [node, const]. A variant value is `_variants`'."""
        extras = [a for a in self._applicable(path) if (a.value is not None or a.null) and path not in self.op.variants]
        for a in extras:
            schema = {**built, "components": {"schemas": self.components}} if isinstance(built, dict) else built
            if jsonschema.Draft202012Validator(schema).is_valid(None if a.null else a.value):
                raise ValueError(f"stale ALLOWED at {path}: the source now accepts "
                                 f"{'null' if a.null else repr(a.value)}; remove the entry")
        if not extras:
            return built
        return {"anyOf": [built, *({"const": None if a.null else a.value, "x-alek-extension": _note(a)}
                                   for a in extras)]}


def _note(a: Allowed) -> str:
    return f"{a.reason} ({a.date})"


def _is_body_path(path: str) -> bool:
    return path != "query" and not path.startswith("header:")


def _check_allowed_kinds(op: Operation) -> None:
    for a in [*op.allowed, *(a for r in op.responses for a in r.allowed)]:
        if not a.reason.strip() or not a.date:
            raise ValueError(f"ALLOWED at {a.path!r} without a reason or a date")
        if (a.value is not None) + (a.field is not None) + a.null != 1:
            raise ValueError(f"ALLOWED at {a.path!r}: exactly one of value / field / null")
        if a.variant is not None and a.field is not None and a.schema is None:
            raise ValueError(f"ALLOWED at {a.path!r}: a variant narrows a value, null or new field only")
        if a.path.startswith("header:") and a.value is None:
            raise ValueError(f"ALLOWED at {a.path!r}: a header allows values only")
        if a.path == "query" and (a.field is None or a.schema is None):
            raise ValueError("ALLOWED at 'query': a query parameter needs a field (its name) and a schema")
    for r in op.responses:
        for a in r.allowed:
            if a.path.startswith("header:"):
                raise ValueError(f"response ALLOWED at {a.path!r}: a response header is not supported")
            if a.field is not None and a.schema is None:
                raise ValueError(f"response ALLOWED at {a.path!r}: a field needs a schema (the provider sends it "
                                 f"although the source lacks it; a deprecated field is accepted in a response anyway)")


def _response_schema(spec: dict, source_op: dict, op: Operation, resp: Response) -> Any:
    """The source schema of one response (a `$ref` to `components/responses` resolved)."""
    item = (source_op.get("responses") or {}).get(resp.status)
    seen = 0
    while isinstance(item, dict) and "$ref" in item:
        item = _pointer(spec, item["$ref"])
        seen += 1
        if seen > 10:
            raise ValueError("a response $ref chain does not end")
    media = ((item or {}).get("content") or {}).get(resp.media) or {}
    if "schema" not in media:
        raise ValueError(f"{op.method} {op.path}: no {resp.status} {resp.media} response in the source")
    return media["schema"]


def _header_parameter(name: str, literals: list[str], op: Operation) -> dict:
    """A comma-separated header (style simple) whose items are the SDK's literals plus the ALLOWED values."""
    items: dict[str, Any] = {"type": "string", "enum": sorted(set(literals))}
    extras = [a for a in op.allowed if a.path == f"header:{name}"]
    for a in extras:
        if a.value in literals:
            raise ValueError(f"stale ALLOWED at {a.path}: the source now has {a.value!r}; remove the entry")
    if extras:
        items = {"anyOf": [items, *({"const": a.value, "x-alek-extension": _note(a)} for a in extras)]}
    return {"name": name, "in": "header", "required": False, "style": "simple", "explode": False,
            "schema": {"type": "array", "items": items}}


def _reachable(paths: dict, components: dict) -> dict:
    """The components the operations reach (a collapsed union can leave one behind)."""
    keep: set[str] = set()
    todo: set[str] = set()
    _collect_refs(paths, todo)
    while todo:
        name = todo.pop().rsplit("/", 1)[-1]
        if name not in keep:
            keep.add(name)
            _collect_refs(components[name], todo)
    return {name: schema for name, schema in components.items() if name in keep}


def _collect_refs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            out.add(node["$ref"])
        for value in node.values():
            _collect_refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, out)


def _inline(spec: dict, node: Any, depth: int = 0) -> Any:
    """The node with every `$ref` replaced by its target (parameters and their schemas are small and acyclic)."""
    if depth > 50:
        raise ValueError("a $ref chain in a parameter does not end")
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(spec, _pointer(spec, node["$ref"]), depth + 1)
        return {k: _inline(spec, v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(spec, v, depth + 1) for v in node]
    return node


def _parameters(spec: dict, item: dict, source_op: dict, op: Operation) -> list[dict]:
    """The parameters we keep, sorted by location and name: the declared headers (as the source writes them), every
    path parameter and the listed query parameters (inlined, documentation keys dropped). Path-level parameters apply
    to the operation unless it redeclares them."""
    declared: dict[tuple[str, str], dict] = {}
    for raw in [*(item.get("parameters") or []), *(source_op.get("parameters") or [])]:
        param = _inline(spec, raw) if "$ref" in raw else raw
        declared[(str(param.get("in")), str(param.get("name")))] = param
    absent = sorted(set(op.query) - {name for where, name in declared if where == "query"})
    if absent:
        raise ValueError(f"{op.method} {op.path}: listed query parameters absent from the source: {absent}")
    in_path = {name for where, name in declared if where == "path"}
    templated = set(re.findall(r"\{([^}]+)\}", op.path))
    if in_path != templated:
        raise ValueError(f"{op.method} {op.path}: path parameters {sorted(in_path)} do not match the template")
    kept = []
    for (where, name), param in sorted(declared.items()):
        if where == "header" and name in op.headers:
            kept.append(param)
        elif where == "path" or where == "query" and name in op.query:
            kept.append(normalize(_plain(_inline(spec, param))))
    extra = [a for a in op.allowed if a.path == "query"]
    for a in extra:
        if ("query", a.field) in declared:
            raise ValueError(f"stale ALLOWED query parameter {a.field!r} of {op.method} {op.path}: the source now "
                             f"declares it; remove the entry")
        kept.append({"name": a.field, "in": "query", "required": False, "schema": _plain(dict(a.schema or {})),
                     "x-alek-extension": _note(a)})
    if extra:
        kept.sort(key=lambda p: (str(p.get("in")), str(p.get("name"))))
    return kept


def build_subset(spec: dict, provider: Provider, *, source: str, version: str, retrieved: str,
                 transforms: tuple[str, ...] = _TRANSFORMS) -> dict:
    """The subset document of `provider` from `spec` (an OpenAPI document; an SDK or discovery source is converted to
    one first)."""
    paths: dict[str, Any] = {}
    registry = _Registry()
    for op in provider.operations:
        _check_allowed_kinds(op)
        item = (spec.get("paths") or {}).get(op.path) or {}
        source_op = item.get(op.method)
        if source_op is None:
            raise ValueError(f"operation {op.method} {op.path} absent from the source")
        built_op: dict[str, Any] = {}
        if op.fields and op.raw_body:
            raise ValueError(f"{op.method} {op.path}: body fields and a raw body are exclusive")
        if op.fields:
            media = ((source_op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
            if "schema" not in media:
                raise ValueError(f"{op.method} {op.path}: no application/json request body")
            body = _Builder(spec, op, registry=registry).body(media["schema"])
            built_op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
        elif op.raw_body:
            media = ((source_op.get("requestBody") or {}).get("content") or {}).get(op.raw_body) or {}
            if "schema" not in media:
                raise ValueError(f"{op.method} {op.path}: no {op.raw_body} request body")
            if op.variants or op.open or any(_is_body_path(a.path) for a in op.allowed):
                raise ValueError(f"{op.method} {op.path}: body paths on an operation with a raw body")
            built_op["requestBody"] = {
                "required": bool((source_op.get("requestBody") or {}).get("required")),
                "content": {op.raw_body: {"schema": normalize(_plain(_inline(spec, media["schema"])))}},
            }
        else:
            if (source_op.get("requestBody") or {}).get("required"):
                raise ValueError(f"{op.method} {op.path}: the source requires a request body; list its fields")
            if op.variants or op.open or any(_is_body_path(a.path) for a in op.allowed):
                raise ValueError(f"{op.method} {op.path}: body paths on an operation without body fields")
        if source_op.get("operationId"):
            built_op["operationId"] = source_op["operationId"]
        params = _parameters(spec, item, source_op, op)
        if params:
            built_op["parameters"] = params
        for resp in op.responses:
            schema = _Builder(spec, resp, registry=registry, response=True).body(
                _response_schema(spec, source_op, op, resp))
            built_resp = built_op.setdefault("responses", {}).setdefault(
                resp.status, {"description": f"the parts our adapter reads: {resp.reads}", "content": {}})
            built_resp["content"][resp.media] = {"schema": schema}
        paths.setdefault(op.path, {})[op.method] = built_op
    components = _reachable(paths, registry.components)
    if any(p.get("in") in ("path", "query") for item in paths.values() for o in item.values()
           for p in o.get("parameters") or []):
        transforms = transforms + (_PARAMETER_TRANSFORM,)
    if any(op.raw_body for op in provider.operations):
        transforms = transforms + (_RAW_BODY_TRANSFORM,)
    if provider.aliases:
        _check_aliases(provider.aliases, paths)
        transforms = transforms + (_ALIAS_TRANSFORM,)
    servers = spec.get("servers") or ([{"url": provider.server}] if provider.server else [])
    has_responses = any(op.responses for op in provider.operations)
    if has_responses:
        transforms = transforms + _RESPONSE_TRANSFORMS[:1]
    if any(r.media == "text/event-stream" for op in provider.operations for r in op.responses):
        transforms = transforms + _RESPONSE_TRANSFORMS[1:]
    title = f"{provider.name} {'request and response' if has_responses else 'request'} subset"
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": str(version)},
        "x-source": source, "x-retrieved": retrieved, "x-transforms": list(transforms),
        "paths": paths, "components": {"schemas": dict(sorted(components.items()))},
    }
    if servers:
        doc["servers"] = [{"url": s["url"]} for s in servers]
    if provider.aliases:
        doc["x-alek-aliases"] = [{"pattern": a.pattern, "target": a.target, "reason": a.reason}
                                 for a in provider.aliases]
    return doc


def _check_aliases(aliases: tuple[Alias, ...], paths: dict) -> None:
    """Every alias has a reason and a target that names a kept operation (a `/**` target: some kept path under it)."""
    for a in aliases:
        if not a.reason.strip():
            raise ValueError(f"alias {a.pattern!r} without a reason")
        if a.pattern.endswith("/**") != a.target.endswith("/**"):
            raise ValueError(f"alias {a.pattern!r} -> {a.target!r}: both or neither end in /**")
        if a.target.endswith("/**"):
            prefix = a.target[:-2]
            if not any(path.startswith(prefix) for path in paths):
                raise ValueError(f"alias target {a.target!r}: no kept operation under it")
        elif a.target not in paths:
            raise ValueError(f"alias target {a.target!r} is not a kept operation path")


def render(doc: dict) -> str:
    """Deterministic YAML: sorted keys, block style."""
    return yaml.safe_dump(doc, sort_keys=True, allow_unicode=True, default_flow_style=False, width=120)


# ----------------------------------------------------------------------------------------------- OData source
_KEY_SEGMENT = "^[^:]+$"
_STRING_LITERAL = "^([^']|'')*$"


def odata_document(spec: dict, operations: tuple[Operation, ...]) -> dict:
    """The spec with `_ODATA_TRANSFORMS` applied to its component schemas and the kept operations (in place: the
    source is parsed for this run only). A 45 MB spec: a few seconds."""
    for path in sorted({op.path for op in operations}):
        item = (spec.get("paths") or {}).get(path) or {}
        _odata_schema_walk(item)
        _odata_path_parameters(spec, path, item)
    for schema in ((spec.get("components") or {}).get("schemas") or {}).values():
        _odata_schema_walk(schema)
    return spec


def _odata_path_parameters(spec: dict, path: str, item: dict) -> None:
    """A key segment's parameter takes no ':'; one inside an OData string literal has its quotes doubled. A parameter
    given by $ref is inlined first, so the change stays with this path."""
    literals = set(re.findall(r"'\{([^}]+)\}'", path))
    for holder in [item, *(item[m] for m in ("get", "put", "post", "patch", "delete") if isinstance(item.get(m), dict))]:
        params = holder.get("parameters")
        if not isinstance(params, list):
            continue
        for i, raw in enumerate(params):
            param = _inline(spec, raw) if "$ref" in raw else raw
            if param.get("in") != "path":
                continue
            name = str(param.get("name"))
            pattern = _STRING_LITERAL if name in literals else _KEY_SEGMENT if name.endswith("-id") else None
            if pattern is None:
                continue
            schema = dict(param.get("schema") or {})
            if schema.get("type") not in ("string", ["string", "null"]) or "pattern" in schema:
                raise ValueError(f"{path}: path parameter {name} is not a plain string: {_canon(schema)[:200]}")
            params[i] = {**param, "schema": {**schema, "pattern": pattern}}


def _odata_schema_walk(node: Any) -> None:
    """Rewrite a schema subtree in place. Property names (the keys of `properties`) are never treated as keywords."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        _odata_node(current)
        for key, value in current.items():
            if key == "properties" and isinstance(value, dict):
                stack.extend(value.values())
            else:
                stack.append(value)


def _odata_node(node: dict) -> None:
    for key in [k for k in node if k == "discriminator" or k.startswith("x-ms-")]:
        del node[key]
    if isinstance(node.get("properties"), dict):
        node["properties"].pop("@odata.type", None)
    if node.get("format") in ("int16", "int32", "int64") and node.get("type") == "number":
        node["type"] = "integer"
    if isinstance(node.get("required"), list) and "@odata.type" in node["required"]:
        node["required"] = [r for r in node["required"] if r != "@odata.type"]
        if not node["required"]:
            del node["required"]
    for key in ("anyOf", "oneOf"):
        if isinstance(node.get(key), list):
            node[key] = [{"type": "null"} if _null_member(m) else m for m in node[key]]
    if node.get("nullable") is True:
        del node["nullable"]
        kind = node.get("type")
        if isinstance(kind, str):
            node["type"] = [kind, "null"]
            if isinstance(node.get("enum"), list) and None not in node["enum"]:
                node["enum"] = [*node["enum"], None]
        elif "$ref" not in node and not any(k in node for k in ("anyOf", "oneOf", "allOf")):
            raise ValueError(f"nullable without a type: {_canon(node)[:200]}")
        else:
            raise ValueError(f"nullable next to a reference or a union: {_canon(node)[:200]}")
    elif "nullable" in node:
        del node["nullable"]


def _null_member(member: Any) -> bool:
    """`{type: object, nullable: true}` (documentation keys aside): the null half of a nullable reference."""
    if not isinstance(member, dict) or member.get("nullable") is not True:
        return False
    return {k for k in member if k not in ("description", "title")} == {"type", "nullable"} \
        and member.get("type") == "object"


# ------------------------------------------------------------------------------------------------------ SDK source
def _import(dotted: str) -> Any:
    module, _, name = dotted.partition(":")
    return getattr(importlib.import_module(module), name)


def _literal_values(tp: Any) -> list[str]:
    origin = typing.get_origin(tp)
    if origin is typing.Literal:
        return [v for v in typing.get_args(tp) if isinstance(v, str)]
    out: list[str] = []
    for arg in typing.get_args(tp):
        out += _literal_values(arg)
    return out


def _walk_types(tp: Any, typeddicts: dict[str, type], models: set[str]) -> None:
    """Every TypedDict reachable from `tp` (by name), and every pydantic model a union offers next to them."""
    if typing_extensions.is_typeddict(tp):  # the SDK's TypedDicts are typing_extensions'
        if tp.__name__ in typeddicts:
            return
        typeddicts[tp.__name__] = tp
        for hint in typing.get_type_hints(tp, include_extras=True).values():
            _walk_types(hint, typeddicts, models)
        return
    if isinstance(tp, type) and issubclass(tp, pydantic.BaseModel):
        models.add(tp.__name__)
        return
    for arg in typing.get_args(tp):
        _walk_types(arg, typeddicts, models)


def _aliases(td: type) -> dict[str, str]:
    out = {}
    for name, hint in typing.get_type_hints(td, include_extras=True).items():
        for meta in getattr(hint, "__metadata__", ()):
            alias = getattr(meta, "alias", None)
            if alias:
                out[name] = alias
    return out


def sdk_document(op: Operation, version: str) -> dict:
    """An OpenAPI document for one SDK operation: its request TypedDict's pydantic schema, header fields dropped,
    response-model union members dropped, header parameters from the headers' Literal types."""
    root = _import(op.sdk_body)
    typeddicts: dict[str, type] = {}
    models: set[str] = set()
    _walk_types(root, typeddicts, models)
    schema = pydantic.TypeAdapter(root).json_schema(ref_template=_REF_PREFIX + "{model}")
    defs = schema.pop("$defs", {})
    root_name = root.__name__
    defs[root_name] = schema
    for td_name, td in typeddicts.items():
        for fname, alias in _aliases(td).items():
            if op.header_aliases.get(fname) != alias:
                raise ValueError(f"{td_name}.{fname} has an alias {alias!r} the table does not know: the SDK "
                                 f"renames it on the wire; add it to header_aliases if it is a header, else extend "
                                 f"the extractor")
            target = defs.get(td_name)
            if target is None:
                raise ValueError(f"no schema for {td_name}")
            (target.get("properties") or {}).pop(fname, None)
            if fname in (target.get("required") or []):
                target["required"] = [r for r in target["required"] if r != fname]

    def strip_models(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: strip_models(v) for k, v in node.items()}
            for key in ("anyOf", "oneOf"):
                if key in node:
                    node[key] = [m for m in node[key] if not (
                        isinstance(m, dict) and m.get("$ref", "").rsplit("/", 1)[-1] in models)]
            return node
        if isinstance(node, list):
            return [strip_models(v) for v in node]
        return node

    defs = {name: strip_models(s) for name, s in defs.items() if name not in models}
    params = [_header_parameter(name, _literal_values(_import(tp)), op) for name, tp in sorted(op.headers.items())]
    source_op: dict[str, Any] = {
        "requestBody": {"content": {"application/json": {"schema": {"$ref": _REF_PREFIX + root_name}}}},
    }
    if params:
        source_op["parameters"] = params
    for resp in op.responses:
        name, schemas = _sdk_response_schemas(resp)
        for def_name, schema in schemas.items():
            # A response model nested in a request type's model is left in the request's $defs (unreached once
            # the models are dropped from the unions): the same schema, still open.
            if def_name in defs and _canon(_close_models(defs[def_name])) != _canon(schema):
                raise ValueError(f"SDK schema {def_name} differs between the request and a response")
            defs[def_name] = schema
        source_op.setdefault("responses", {}).setdefault(resp.status, {"content": {}})["content"][resp.media] = {
            "schema": {"$ref": _REF_PREFIX + name}}
    return {"openapi": "3.1.0", "info": {"version": version},
            "paths": {op.path: {op.method: source_op}}, "components": {"schemas": defs}}


def _sdk_response_schemas(resp: Response) -> tuple[str, dict[str, Any]]:
    """The JSON Schemas of an SDK response type (its name, and every schema by name), each model closed: the SDK's
    response models accept extra fields, the contract does not."""
    if not resp.sdk_model:
        raise ValueError(f"an SDK source needs sdk_model on its {resp.status} {resp.media} response")
    name = resp.sdk_model.partition(":")[2]
    schema = pydantic.TypeAdapter(_import(resp.sdk_model)).json_schema(ref_template=_REF_PREFIX + "{model}")
    defs = schema.pop("$defs", {})
    if name in defs and _canon(defs[name]) != _canon(schema):
        raise ValueError(f"SDK response type {name} has two schemas")
    defs[name] = schema
    return name, {def_name: _close_models(s) for def_name, s in defs.items()}


def _close_models(node: Any) -> Any:
    """The schema with `additionalProperties: true` dropped from every object that declares properties (the builder
    then closes it)."""
    if isinstance(node, dict):
        node = {k: _close_models(v) for k, v in node.items()}
        if "properties" in node and node.get("additionalProperties") is True:
            del node["additionalProperties"]
        return node
    if isinstance(node, list):
        return [_close_models(v) for v in node]
    return node


def sdk_subset(provider: Provider, retrieved: str) -> dict:
    """The subset of an SDK-sourced provider, from the installed package (offline)."""
    version = importlib.import_module(provider.sdk).__version__
    docs = [sdk_document(op, version) for op in provider.operations]
    merged: dict[str, Any] = {"paths": {}, "components": {"schemas": {}}}
    for doc in docs:
        for path, item in doc["paths"].items():
            merged["paths"].setdefault(path, {}).update(item)
        merged["components"]["schemas"].update(doc["components"]["schemas"])
    transforms = _TRANSFORMS + _SDK_TRANSFORMS
    if any(op.responses for op in provider.operations):
        transforms += _SDK_RESPONSE_TRANSFORMS
    return build_subset(merged, provider, source=f"{provider.sdk}-sdk-python@{version}", version=version,
                        retrieved=retrieved, transforms=transforms)


# --------------------------------------------------------------------------------------------------- OpenAPI source
def spec_url(provider: Provider, ref: str) -> str:
    return f"https://raw.githubusercontent.com/{provider.repo}/{ref}/{provider.spec_file}"


def head_commit(repo: str) -> str:
    """The commit the default branch points at (pins the source)."""
    out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
                         check=True, capture_output=True, text=True, timeout=60).stdout
    sha = out.split()[0] if out.split() else ""
    if len(sha) != 40:
        raise RuntimeError(f"no HEAD commit for {repo}: {out!r}")
    return sha


def openapi_subset(provider: Provider, ref: str, retrieved: str, fetch: Callable[[str], bytes] = fetch_url) -> dict:
    """The subset of a provider whose source is a file on GitHub (OpenAPI, or a discovery document), pinned by `ref`."""
    url = spec_url(provider, ref)
    spec = parse(fetch(url))
    transforms = _TRANSFORMS
    if provider.discovery:
        spec = discovery_document(spec, provider.operations)
        transforms = _TRANSFORMS + _DISCOVERY_TRANSFORMS
    if provider.odata:
        spec = odata_document(spec, provider.operations)
        transforms = _TRANSFORMS + _ODATA_TRANSFORMS
    version = (spec.get("info") or {}).get("version", "")
    return build_subset(spec, provider, source=url, version=version, retrieved=retrieved, transforms=transforms)


# ------------------------------------------------------------------------------------------------- discovery source
def _typed(value: Any, kind: str | None, where: str) -> Any:
    """A discovery default/minimum/maximum (written as a string) as a value of the parameter's type."""
    if not isinstance(value, str) or kind in (None, "string"):
        return value
    if kind == "integer":
        return int(value)
    if kind == "number":
        return float(value)
    if kind == "boolean" and value in ("true", "false"):
        return value == "true"
    raise ValueError(f"{where}: {value!r} is not a {kind}")


# An integer format's range: JSON Schema does not enforce `format`, so the range becomes minimum/maximum.
_INTEGER_RANGES = {
    "int32": (-2**31, 2**31 - 1), "uint32": (0, 2**32 - 1), "int64": (-2**63, 2**63 - 1), "uint64": (0, 2**64 - 1),
}


def _discovery_parameter(name: str, param: dict) -> dict:
    location = param.get("location")
    if location not in ("path", "query"):
        raise ValueError(f"parameter {name}: location {location!r} is not converted")
    kind = param.get("type")
    schema: dict[str, Any] = {k: param[k] for k in ("type", "format", "enum", "pattern") if k in param}
    if kind == "integer" and param.get("format") in _INTEGER_RANGES:
        schema["minimum"], schema["maximum"] = _INTEGER_RANGES[param["format"]]
    for key in ("default", "minimum", "maximum"):
        if key in param:
            schema[key] = _typed(param[key], kind, f"parameter {name}.{key}")
    if param.get("repeated"):
        schema = {"type": "array", "items": schema}
    out = {"name": name, "in": location, "required": location == "path" or bool(param.get("required")),
           "schema": schema}
    if param.get("deprecated"):
        out["deprecated"] = True
    return out


def discovery_document(discovery: dict, operations: tuple[Operation, ...]) -> dict:
    """An OpenAPI document of the discovery methods the operations name (by method and `/<path>`): each with its own
    and the API-wide path/query parameters. No request body is converted: a kept method that takes one fails."""
    wanted = {(op.method, op.path): op for op in operations}
    shared = discovery.get("parameters") or {}
    paths: dict[str, Any] = {}
    for method in _discovery_methods(discovery):
        key = (str(method.get("httpMethod", "")).lower(), "/" + str(method.get("path", "")))
        if key not in wanted:
            continue
        if "{+" in key[1]:
            raise ValueError(f"{method.get('id')}: reserved expansion {{+name}} in {key[1]} is not converted")
        if "request" in method and wanted[key].fields:
            raise ValueError(f"{method.get('id')}: a discovery request body is not converted (extend the converter)")
        params = {**shared, **(method.get("parameters") or {})}
        paths.setdefault(key[1], {})[key[0]] = {
            "operationId": method["id"],
            "parameters": [_discovery_parameter(n, p) for n, p in sorted(params.items())],
        }
    server = (discovery.get("rootUrl", "") + discovery.get("servicePath", "")).rstrip("/")
    version = f"{discovery.get('version', '')}-{discovery.get('revision', '')}"
    return {"openapi": "3.1.0", "info": {"version": version}, "servers": [{"url": server}], "paths": paths,
            "components": {"schemas": {}}}


# ------------------------------------------------------------------------------------------------------------- CLI
def write(provider_name: str, doc: dict, out_dir: Path = CONTRACTS_DIR) -> Path:
    text = render(doc)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"{provider_name}: subset is {len(text.encode())} bytes, over the {MAX_BYTES} cap")
    path = out_dir / provider_name / "openapi.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    parser.add_argument("--ref", default="", help="OpenAPI or discovery source: the commit to pin (default: the branch "
                                                   "head)")
    args = parser.parse_args(argv)
    provider = PROVIDERS[args.provider]
    today = _dt.date.today().isoformat()
    if provider.sdk:
        if args.ref:
            raise SystemExit("--ref applies to an OpenAPI source; an SDK source is pinned by the installed version")
        doc = sdk_subset(provider, today)
    else:
        doc = openapi_subset(provider, args.ref or head_commit(provider.repo), today)
    print(f"wrote {write(provider.name, doc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
