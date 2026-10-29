"""
Adapter behavioral contracts.

⚠️  AI MODIFICATION POLICY — READ BEFORE CHANGING ANYTHING IN THIS FILE ⚠️
This file is the authoritative rule repository for adapter behavioral contracts.
Modifying or removing a ContractRule changes what the test suite considers "correct"
adapter behavior — equivalent to removing a safety net, not fixing a bug.

RULE: Do NOT add, modify, or remove any ContractRule (or its validators) without
explicit written confirmation from the user in the current session.
If a test that uses these rules fails, investigate the adapter code first.
Only change this file when the user explicitly approves the contract change.

A ContractRule defines a named invariant that must hold for a given LLMRequest
scenario, expressed as per-provider validator callables. Validators receive the
captured SDK call kwargs and raise AssertionError on violation.

Usage in unit wire tests:
    FORCE_TOOL_USE_SENDS_CORRECT_MODE.validate("claude", captured_kwargs)

Usage in integration tests:
    FORCE_TOOL_USE_SENDS_CORRECT_MODE.validate("grok", stub.captured_kwargs)

Adding a new rule:
    1. Define a ContractRule constant at module level.
    2. Add a validator for each provider that implements the feature.
    3. Import and call .validate() in both unit and integration test files.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict

import google_crc32c
from google.genai import types


# ============================================================================
# Infrastructure
# ============================================================================

@dataclass
class ContractRule:
    """
    A named behavioral invariant for LLM adapter SDK-level translation.

    validators: provider_name -> callable(captured_kwargs: dict) -> None
    Validator raises AssertionError with a descriptive message on violation.
    Providers not listed in validators are silently skipped (feature not applicable).
    """
    name: str
    description: str
    validators: Dict[str, Callable[[dict], None]] = field(default_factory=dict)

    def validate(self, provider: str, captured_kwargs: dict) -> None:
        """Run the validator for provider; no-op if no rule defined for that provider."""
        validator = self.validators.get(provider)
        if validator:
            validator(captured_kwargs)


# ============================================================================
# Assertion helpers — thin wrappers that produce clear failure messages
# ============================================================================

def _eq(actual, expected, msg: str) -> None:
    assert actual == expected, f"{msg}. Got: {actual!r}"


def _not_none(val, msg: str) -> None:
    assert val is not None, msg


def _true(val, msg: str) -> None:
    assert val, msg


def _not_in(key: str, d: dict, msg: str) -> None:
    assert key not in d, f"{msg}. Keys present: {sorted(d.keys())}"


# ============================================================================
# Rules
# ============================================================================

FORCE_TOOL_USE_SENDS_CORRECT_MODE = ContractRule(
    name="FORCE_TOOL_USE_SENDS_CORRECT_MODE",
    description=(
        "When LLMRequest.force_tool_use=True and tools are present, the adapter must "
        "instruct the SDK to require a tool call — the model cannot return plain text. "
        "Each provider has its own API field for this: "
        "Claude: tool_choice={'type':'any'}, Gemini: FunctionCallingConfig.mode='ANY', "
        "Grok/OpenAI: tool_choice='required'."
    ),
    validators={
        "claude": lambda kw: _eq(
            kw.get("tool_choice"),
            {"type": "any"},
            "Claude: tool_choice must be {'type':'any'} when force_tool_use=True",
        ),
        "gemini": lambda kw: (
            _not_none(
                kw.get("config") and kw["config"].tool_config,
                "Gemini: config.tool_config must not be None when force_tool_use=True",
            ),
            _eq(
                kw["config"].tool_config.function_calling_config.mode,
                "ANY",
                "Gemini: function_calling_config.mode must be 'ANY' when force_tool_use=True",
            ),
        ),
        "grok": lambda kw: _eq(
            kw.get("tool_choice"),
            "required",
            "Grok: tool_choice must be 'required' when force_tool_use=True",
        ),
        "openai": lambda kw: _eq(
            kw.get("tool_choice"),
            "required",
            "OpenAI: tool_choice must be 'required' when force_tool_use=True",
        ),
    },
)

GROUNDING_INJECTS_SEARCH_TOOL = ContractRule(
    name="GROUNDING_INJECTS_SEARCH_TOOL",
    description=(
        "When LLMRequest.use_grounding=True, the adapter must inject the provider's "
        "native search tool into the API call. The search tool should be the first "
        "element so it takes priority over domain function tools. "
        "Claude: web_search_20260209 (Sonnet/Opus) or web_search_20250305 (Haiku) in tools list. "
        "Gemini: Tool with google_search in config.tools. "
        "Grok/OpenAI: {'type':'web_search'} tool in tools list."
    ),
    validators={
        "claude": lambda kw: _true(
            any(
                isinstance(t, dict) and t.get("type") in (
                    "web_search_20260209",
                    "web_search_20250305",
                )
                for t in (kw.get("tools") or [])
            ),
            "Claude: web_search tool (20260209 or 20250305) missing from tools when use_grounding=True",
        ),
        "gemini": lambda kw: _true(
            any(
                getattr(t, "google_search", None) is not None
                for t in (getattr(kw.get("config"), "tools", None) or [])
            ),
            "Gemini: GoogleSearch tool missing from config.tools when use_grounding=True",
        ),
        "grok": lambda kw: _true(
            any(
                isinstance(t, dict) and t.get("type") == "web_search"
                for t in (kw.get("tools") or [])
            ),
            "Grok: web_search tool missing from tools when use_grounding=True",
        ),
        "openai": lambda kw: _true(
            any(
                isinstance(t, dict) and t.get("type") == "web_search"
                for t in (kw.get("tools") or [])
            ),
            "OpenAI: web_search tool missing from tools when use_grounding=True",
        ),
    },
)

FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE = ContractRule(
    name="FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE",
    description=(
        "When force_tool_use=True but no tools are provided, the adapter must omit "
        "tool_choice from the API call entirely. Provider APIs return a 400 error "
        "when tool_choice is set but tools is absent or empty."
    ),
    validators={
        "claude": lambda kw: _not_in(
            "tool_choice",
            kw,
            "Claude: tool_choice must be absent when no tools present",
        ),
        "grok": lambda kw: _not_in(
            "tool_choice",
            kw,
            "Grok: tool_choice must be absent when no tools present",
        ),
        "openai": lambda kw: _not_in(
            "tool_choice",
            kw,
            "OpenAI: tool_choice must be absent when no tools present",
        ),
    },
)


# ============================================================================
# Non-LLM adapter rules
#
# ContractRule.validators is a free-keyed string→callable map. For LLM rules
# the key is the provider name; for non-LLM rules it is the adapter name
# (e.g. "gmail"). The captured-input shape is per-adapter — the LLM rules
# above receive SDK kwargs; HTTP-boundary adapters like Gmail receive a
# request record {method, url, headers, params, data}. Each rule documents
# its expected input shape in the description.
# ============================================================================

GMAIL_AUTHORIZATION_HEADER_PRESENT = ContractRule(
    name="GMAIL_AUTHORIZATION_HEADER_PRESENT",
    description=(
        "Every Gmail API request issued by GmailProviderAdapter must carry an "
        "Authorization: Bearer <token> header. Missing header → silent 401 inside "
        "the adapter's try/except blocks → data loss with no clear signal. "
        "Input: captured request record {method, url, headers, params, ...}."
    ),
    validators={
        "gmail": lambda req: (
            _true(
                "Authorization" in req["headers"],
                f"Gmail: missing Authorization header on {req.get('method')} {req.get('url')}",
            ),
            _true(
                req["headers"].get("Authorization", "").startswith("Bearer "),
                f"Gmail: Authorization must use Bearer scheme. Got: {req['headers'].get('Authorization')!r}",
            ),
        ),
    },
)

GMAIL_LIST_EMAILS_PAGE_TOKEN_EXCLUDES_QUERY = ContractRule(
    name="GMAIL_LIST_EMAILS_PAGE_TOKEN_EXCLUDES_QUERY",
    description=(
        "When GmailProviderAdapter.list_emails resumes via page_token, the q= "
        "parameter must be omitted from the /messages list call. Gmail embeds the "
        "original query in pageToken; passing q= alongside silently overrides the "
        "embedded date filter and returns emails outside the requested range. "
        "Input: captured request record for the list-page call (the one whose "
        "params dict contains 'pageToken')."
    ),
    validators={
        "gmail": lambda req: _not_in(
            "q",
            req["params"],
            "Gmail: q= must be absent when pageToken is present in params",
        ),
    },
)


def _has_filter(filters, field_path: str, op_string: str) -> bool:
    """True if any FieldFilter in `filters` matches (field_path, op_string)."""
    return any(
        getattr(f, "field_path", None) == field_path
        and getattr(f, "op_string", None) == op_string
        for f in filters
    )


FIRESTORE_EMAIL_FIND_NEAREST_FILTERS_USER_AND_STATE = ContractRule(
    name="FIRESTORE_EMAIL_FIND_NEAREST_FILTERS_USER_AND_STATE",
    description=(
        "Every find_nearest query issued by FirestoreIndexedEmailRepository must "
        "carry a user_id== AND a state== where-filter. Missing user_id risks "
        "cross-tenant data leakage; missing state returns archived emails to "
        "search results. SECURITY-RELEVANT. "
        "Input: captured call {where_filters: list[FieldFilter], kwargs: dict}."
    ),
    validators={
        "firestore_indexed_email": lambda call: (
            _true(
                _has_filter(call["where_filters"], "user_id", "=="),
                "Firestore indexed_email find_nearest missing user_id== filter (cross-tenant leak risk)",
            ),
            _true(
                _has_filter(call["where_filters"], "state", "=="),
                "Firestore indexed_email find_nearest missing state== filter (archived emails leak into results)",
            ),
        ),
    },
)

FIRESTORE_EMAIL_SAVE_BATCH_COMPOSITE_DOC_ID = ContractRule(
    name="FIRESTORE_EMAIL_SAVE_BATCH_COMPOSITE_DOC_ID",
    description=(
        "FirestoreIndexedEmailRepository.save_batch must write each email at "
        "doc_id = '{user_id}_{email_id}'. The composite ID provides per-user "
        "isolation at the doc-id layer + idempotency on retry. If silently "
        "changed to email_id alone, two users with the same provider message "
        "ID collide; if changed to auto-id, retries duplicate facts. "
        "Input: captured call {doc_id: str, user_id: str, email_id: str}."
    ),
    validators={
        "firestore_indexed_email": lambda call: _eq(
            call["doc_id"],
            f"{call['user_id']}_{call['email_id']}",
            f"Firestore indexed_email batch.set doc_id must be '{{user_id}}_{{email_id}}'",
        ),
    },
)

FIRESTORE_COMPANION_FIND_NEAREST_FILTERS_SESSION_ID = ContractRule(
    name="FIRESTORE_COMPANION_FIND_NEAREST_FILTERS_SESSION_ID",
    description=(
        "Every find_nearest query issued by FirestoreCompanionMemoryRepository "
        "must carry a session_id== where-filter. Missing it risks a companion "
        "surfacing another session's records — a memory-policy violation "
        "(COMPANION_AGENTS_RFC.md §4: 'a session cannot have two memory "
        "policies', which presumes it cannot read another session's either). "
        "Input: captured call {where_filters: list[FieldFilter], kwargs: dict}."
    ),
    validators={
        "firestore_companion_memory": lambda call: _true(
            _has_filter(call["where_filters"], "session_id", "=="),
            "Firestore companion_memory find_nearest missing session_id== filter "
            "(cross-session leak risk)",
        ),
    },
)

FIRESTORE_COMPANION_FIND_NEAREST_FILTERS_ACCOUNT_ID = ContractRule(
    name="FIRESTORE_COMPANION_FIND_NEAREST_FILTERS_ACCOUNT_ID",
    description=(
        "Every find_nearest query issued by FirestoreCompanionMemoryRepository "
        "must ALSO carry an account_id== where-filter, alongside session_id==. "
        "Defense in depth: session_id = f'{platform}:{channel_id}' is not "
        "provably unique across two accounts on the same platform (e.g. two "
        "Slack workspaces), so session_id alone is not a sufficient tenancy "
        "guarantee. Input: captured call {where_filters: list[FieldFilter], "
        "kwargs: dict}."
    ),
    validators={
        "firestore_companion_memory": lambda call: _true(
            _has_filter(call["where_filters"], "account_id", "=="),
            "Firestore companion_memory find_nearest missing account_id== filter "
            "(cross-account leak risk)",
        ),
    },
)

FIRESTORE_COMPANION_SAVE_BATCH_INCLUDES_ACCOUNT_ID = ContractRule(
    name="FIRESTORE_COMPANION_SAVE_BATCH_INCLUDES_ACCOUNT_ID",
    description=(
        "FirestoreCompanionMemoryRepository.save_batch must write a non-empty "
        "account_id on every record. RFC §6: 'account_id is an indexed field "
        "on every record from day one — cheap now, expensive to backfill "
        "later.' Input: captured call {data: dict} (the written document body)."
    ),
    validators={
        "firestore_companion_memory": lambda call: _true(
            bool(call["data"].get("account_id")),
            "Firestore companion_memory batch.set missing account_id",
        ),
    },
)

FIRESTORE_COMPANION_CACHE_SAVE_INCLUDES_ACCOUNT_ID = ContractRule(
    name="FIRESTORE_COMPANION_CACHE_SAVE_INCLUDES_ACCOUNT_ID",
    description=(
        "FirestoreCompanionCacheRepository.save_summary must write a non-empty "
        "account_id on every cache doc — same tenancy-attribution convention as "
        "FIRESTORE_COMPANION_SAVE_BATCH_INCLUDES_ACCOUNT_ID. Input: captured call "
        "{data: dict} (the written document body)."
    ),
    validators={
        "firestore_companion_cache": lambda call: _true(
            bool(call["data"].get("account_id")),
            "Firestore companion_cache save_summary missing account_id",
        ),
    },
)

NODE_DOCX_SPEC_PASSED_VIA_STDIN = ContractRule(
    name="NODE_DOCX_SPEC_PASSED_VIA_STDIN",
    description=(
        "NodeDocxRunner must pass the spec_json payload via subprocess stdin "
        "(proc.communicate(input=...)) — NOT as a CLI argument. Spec payloads "
        "can be tens-of-KB; passing as argv risks E2BIG on long documents and "
        "leaks the spec into process listings. "
        "Input: captured call {exec_args: tuple, stdin_inputs: list[bytes], "
        "expected_spec_bytes: bytes}."
    ),
    validators={
        "node_docx_runner": lambda call: (
            _true(
                call["expected_spec_bytes"] in call["stdin_inputs"],
                f"node_docx_runner: spec_json must be written to subprocess stdin. "
                f"Stdin inputs seen: {call['stdin_inputs']!r}",
            ),
            _true(
                not any(
                    call["expected_spec_bytes"].decode("utf-8") in (arg if isinstance(arg, str) else "")
                    for arg in call["exec_args"]
                ),
                "node_docx_runner: spec_json must NOT appear in exec argv (E2BIG risk)",
            ),
        ),
    },
)

NODE_DOCX_INVOKED_WITH_NODE_AND_SCRIPT_PATH = ContractRule(
    name="NODE_DOCX_INVOKED_WITH_NODE_AND_SCRIPT_PATH",
    description=(
        "NodeDocxRunner must invoke `node <temp-script.js>` where the script "
        "path resolves under the project's docx_generator/ directory. Running "
        "the script from any other directory breaks `require('docx')` because "
        "Node resolves node_modules from the script's parent directory. "
        "Input: captured call {exec_args: tuple, expected_dir_substring: str}."
    ),
    validators={
        "node_docx_runner": lambda call: (
            _true(
                len(call["exec_args"]) >= 2 and call["exec_args"][0] == "node",
                f"node_docx_runner: first exec arg must be 'node'. Got: {call['exec_args']!r}",
            ),
            _true(
                str(call["exec_args"][1]).endswith(".js"),
                f"node_docx_runner: second exec arg must be a .js path. Got: {call['exec_args'][1]!r}",
            ),
            _true(
                call["expected_dir_substring"] in str(call["exec_args"][1]),
                f"node_docx_runner: script path must live under {call['expected_dir_substring']!r} "
                f"for node_modules resolution. Got: {call['exec_args'][1]!r}",
            ),
        ),
    },
)

OPENAI_REALTIME_STRIPS_CACHE_BOUNDARY = ContractRule(
    name="OPENAI_REALTIME_STRIPS_CACHE_BOUNDARY",
    description=(
        "OpenAIRealtimeAdapter.open() must never forward the literal cache-boundary "
        "marker used elsewhere in the prompt-caching pipeline into a live realtime "
        "session's instructions field — it has no meaning to the Realtime API and "
        "would leak an internal prompt-assembly artifact into the model's context. "
        "Input: captured session.update message {type: str, session: dict}."
    ),
    validators={
        "openai_realtime": lambda kw: _true(
            "CACHE_BOUNDARY" not in kw["session"]["instructions"],
            "openai_realtime: session.update instructions must not contain CACHE_BOUNDARY",
        ),
    },
)

OPENAI_REALTIME_USES_GA_SESSION_SHAPE = ContractRule(
    name="OPENAI_REALTIME_USES_GA_SESSION_SHAPE",
    description=(
        "OpenAIRealtimeAdapter.open() must send the GA Realtime API session shape "
        "(session.type == 'realtime', output_modalities as a nested audio-config field) "
        "and never the pre-GA shape (a top-level 'modalities' array), which spike 0.1/0.2 "
        "found the live API silently rejects with no error surfaced to the caller. "
        "Input: captured session.update message {type: str, session: dict}."
    ),
    validators={
        "openai_realtime": lambda kw: (
            _eq(
                kw["session"]["type"],
                "realtime",
                "openai_realtime: session.update must set session.type='realtime'",
            ),
            _not_in(
                "modalities",
                kw["session"],
                "openai_realtime: session must not use the pre-GA top-level 'modalities' key",
            ),
        ),
    },
)

OPENAI_REALTIME_BARGE_IN_IS_CLIENT_OWNED = ContractRule(
    name="OPENAI_REALTIME_BARGE_IN_IS_CLIENT_OWNED",
    description=(
        "OpenAIRealtimeAdapter.open() must configure semantic_vad with the provider's "
        "auto-interrupt OFF. VoiceSessionService owns barge-in (clear -> cancel -> "
        "truncate); a provider auto-cancel racing our own response.cancel can land a "
        "cancel on nothing, and that provider error ends the call. Auto-reply is off too: "
        "the client places the persona anchor after each turn, then starts the reply. semantic_vad (not "
        "server_vad) so a mid-thought pause does not end the caller's turn. "
        "Input: captured session.update message {type: str, session: dict}."
    ),
    validators={
        "openai_realtime": lambda kw: (
            _eq(
                kw["session"]["audio"]["input"]["turn_detection"]["type"],
                "semantic_vad",
                "openai_realtime: turn_detection must be semantic_vad",
            ),
            _eq(
                kw["session"]["audio"]["input"]["turn_detection"]["interrupt_response"],
                False,
                "openai_realtime: provider auto-interrupt must be off (client owns barge-in)",
            ),
            _eq(
                kw["session"]["audio"]["input"]["turn_detection"]["create_response"],
                False,
                "openai_realtime: provider auto-reply must be off (client anchors, then replies)",
            ),
        ),
    },
)

def _validate_openai_realtime_tools_shape(kw: dict) -> None:
    session = kw["session"]
    tools = session.get("tools")
    if not tools:
        _not_in(
            "tools", session,
            "openai_realtime: session must carry no 'tools' key when no tools were passed",
        )
        return
    _true(
        all(
            tool.get("type") == "function"
            and "name" in tool and "description" in tool and "parameters" in tool
            for tool in tools
        ),
        "openai_realtime: every session.tools entry must be "
        "{'type': 'function', 'name', 'description', 'parameters'}",
    )
    _eq(
        session.get("tool_choice"), "auto",
        "openai_realtime: tool_choice must be 'auto' when tools are present",
    )


OPENAI_REALTIME_TOOLS_ARE_FUNCTION_SHAPED = ContractRule(
    name="OPENAI_REALTIME_TOOLS_ARE_FUNCTION_SHAPED",
    description=(
        "OpenAIRealtimeAdapter.open() must translate the neutral tool declaration "
        "(BaseAgent._build_delegate_tool_declaration: name/description/parameters) into "
        "OpenAI Realtime's function shape - every session.tools entry needs type='function' "
        "alongside the original name/description/parameters - with tool_choice='auto'. When no "
        "tools are passed, session must carry no 'tools' key at all. "
        "Input: captured session.update message {type: str, session: dict}."
    ),
    validators={
        "openai_realtime": _validate_openai_realtime_tools_shape,
    },
)

OPENAI_REALTIME_TRUNCATE_SHAPE = ContractRule(
    name="OPENAI_REALTIME_TRUNCATE_SHAPE",
    description=(
        "OpenAIRealtimeAdapter.truncate() must send conversation.item.truncate with "
        "item_id, content_index=0 and an integer audio_end_ms — over WebSocket the "
        "server cannot know what was played, so the client cuts the unheard tail. "
        "Input: captured client event dict."
    ),
    validators={
        "openai_realtime": lambda kw: (
            _eq(kw["type"], "conversation.item.truncate",
                "openai_realtime: truncate must send conversation.item.truncate"),
            _eq(kw["content_index"], 0, "openai_realtime: truncate content_index must be 0"),
            _true(isinstance(kw["audio_end_ms"], int) and kw["audio_end_ms"] >= 0,
                  "openai_realtime: audio_end_ms must be a non-negative int"),
            _true(bool(kw["item_id"]), "openai_realtime: truncate needs an item_id"),
        ),
    },
)

XAI_REALTIME_STRIPS_CACHE_BOUNDARY = ContractRule(
    name="XAI_REALTIME_STRIPS_CACHE_BOUNDARY",
    description=(
        "XaiRealtimeAdapter.open() must never forward the prompt-caching boundary marker "
        "into the session instructions - a session prompt is set once and the marker would "
        "be spoken. Input: captured session.update message {type: str, session: dict}."
    ),
    validators={
        "xai_realtime": lambda kw: _true(
            "CACHE_BOUNDARY" not in kw["session"]["instructions"],
            "xai_realtime: session.update instructions must not contain CACHE_BOUNDARY",
        ),
    },
)

XAI_REALTIME_TURN_DETECTION = ContractRule(
    name="XAI_REALTIME_TURN_DETECTION",
    description=(
        "XaiRealtimeAdapter.open() must configure xAI's top-level server_vad with auto-reply "
        "and auto-interrupt ON: the adapter implements PROVIDER turn ownership only - xAI starts "
        "every reply to the caller and stops it when talked over (VOICE_MULTI_PROVIDER_RFC "
        "§4.3/§4.4; xAI ignores create_response=false anyway, decisions/voice_xai_protocol_probe.md). "
        "Input: captured session.update message {type: str, session: dict}."
    ),
    validators={
        "xai_realtime": lambda kw: (
            _eq(kw["session"]["turn_detection"]["type"], "server_vad",
                "xai_realtime: turn_detection must be server_vad"),
            _eq(kw["session"]["turn_detection"]["create_response"], True,
                "xai_realtime: provider auto-reply must be on (provider owns turns)"),
            _eq(kw["session"]["turn_detection"]["interrupt_response"], True,
                "xai_realtime: provider auto-interrupt must be on (provider owns barge-in)"),
        ),
    },
)


def _text_follows_function_output(items) -> bool:
    """OpenAI/Grok Responses API: items are a flat list, not nested message blocks.
    True iff a user-role item with non-empty text content appears after the first
    function_call_output item."""
    try:
        i_out = next(i for i, it in enumerate(items) if it.get("type") == "function_call_output")
    except StopIteration:
        return False
    return any(
        it.get("role") == "user" and str(it.get("content") or "").strip()
        for it in items[i_out + 1:]
    )


NOTE_AFTER_TOOL_RESULT_SURVIVES_CONVERSION = ContractRule(
    name="NOTE_AFTER_TOOL_RESULT_SURVIVES_CONVERSION",
    description=(
        "LONG_RUNNING_TURNS_RFC plan delta D6: DelegationEngine appends a short marker "
        "note (e.g. the meanwhile-chat note, or the wrap-up note) as a text part on the "
        "SAME user message that carries a tool result, rather than opening a second user "
        "message. Every adapter's converter must preserve that text part — never drop it, "
        "never reorder it before the tool result. "
        "Claude: the user message's content blocks include a tool_result block, and the "
        "LAST block is a non-empty text block. "
        "Gemini: the last Content (role='user') has both a function_response part and a "
        "non-empty text part. "
        "OpenAI/Grok: a user-role item with non-empty text content appears after the "
        "function_call_output item in the flat `input` list. "
        "Input — Claude: {'messages': <converted messages list>}. Gemini: "
        "{'contents': <converted contents list>}. OpenAI/Grok: {'input': <converted input "
        "list>}. Each is exactly what `_convert_messages`/`_convert_input` returns — the "
        "same object the adapter passes to the SDK as `messages=`/`contents=`/`input=`."
    ),
    validators={
        "claude": lambda kw: _true(
            any(
                isinstance(msg.get("content"), list)
                and any(b.get("type") == "tool_result" for b in msg["content"])
                and msg["content"][-1].get("type") == "text"
                and (msg["content"][-1].get("text") or "").strip()
                for msg in (kw.get("messages") or [])
                if msg.get("role") == "user"
            ),
            "Claude: no user message found with a tool_result block followed by a "
            "non-empty trailing text block",
        ),
        "gemini": lambda kw: _true(
            any(
                getattr(content, "role", None) == "user"
                and any(getattr(p, "function_response", None) for p in content.parts)
                and any(getattr(p, "text", None) for p in content.parts)
                for content in (kw.get("contents") or [])
            ),
            "Gemini: no user Content found with both a function_response part and a "
            "text part",
        ),
        "openai": lambda kw: _true(
            _text_follows_function_output(kw.get("input") or []),
            "OpenAI: no user-role item with non-empty text found after a "
            "function_call_output item",
        ),
        "grok": lambda kw: _true(
            _text_follows_function_output(kw.get("input") or []),
            "Grok: no user-role item with non-empty text found after a "
            "function_call_output item",
        ),
    },
)


# ---- Structured output next to client tools (decisions/claude_deliver_response_string_tool.md) -------------------

def _schema_keys(schema) -> set:
    """Every property name a JSON schema declares, at any depth."""
    keys: set = set()
    if isinstance(schema, dict):
        for name, sub in (schema.get("properties") or {}).items():
            keys.add(name)
            keys |= _schema_keys(sub)
        for value in schema.values():
            if isinstance(value, (dict, list)):
                keys |= _schema_keys(value)
    elif isinstance(schema, list):
        for item in schema:
            keys |= _schema_keys(item)
    return keys


def _claude_deliver_tool(kw: dict) -> dict:
    tools = kw.get("tools") or []
    _true(tools, "Claude: no tools were sent")
    last = tools[-1]
    _eq(last.get("name"), "deliver_response", "Claude: the last tool must be the synthesized deliver_response")
    return last


def _check_claude_deliver_tool(kw: dict) -> None:
    _not_in("format", kw.get("output_config") or {}, "Claude: output_config.format must not be sent with client tools")
    last = _claude_deliver_tool(kw)
    schema = last.get("input_schema") or {}
    _eq(schema.get("required"), ["response"], "Claude: deliver_response must require exactly `response`")
    props = schema.get("properties") or {}
    _eq(sorted(props), ["response"], "Claude: deliver_response must have exactly one property")
    _eq(props["response"].get("type"), "string", "Claude: deliver_response.response must be a string")
    # `response` is the tool's own property; a schema key of the same name is not a leak.
    leaked = (_schema_keys(kw["expected_schema"]) - {"response"}) & (_schema_keys(schema) - {"response"})
    _true(not leaked, f"Claude: the answer schema's keys leaked into deliver_response.input_schema: {sorted(leaked)}")


def _check_grok_deliver_tool(kw: dict) -> None:
    tools = kw.get("tools") or []
    delivers = [t for t in tools if t.get("type") == "function" and t.get("name") == "deliver_response"]
    _eq(len(delivers), 1, "Grok: exactly one deliver_response function tool must be sent")
    _eq(delivers[0].get("parameters"), kw["expected_parameters"],
        "Grok: deliver_response.parameters must be the response schema")
    _not_in("text", kw, "Grok: no text format may be sent next to the deliver_response tool")
    _eq(kw.get("tool_choice"), "required", "Grok: tool_choice must be 'required' with deliver_response")


SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL = ContractRule(
    name="SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL",
    description=(
        "A dict response_schema together with client tools (and no grounding): the structured answer moves off "
        "the text channel onto a synthesized deliver_response tool. "
        "Claude: no output_config.format; the LAST tool is deliver_response with exactly one required string "
        "property `response` (the schema's keys appear nowhere in its input_schema). "
        "Grok: a function tool deliver_response whose parameters are the schema, no text format, "
        "tool_choice='required'. Input: the captured kwargs plus `expected_schema` (Claude) / "
        "`expected_parameters` (Grok: GrokAdapter._to_json_schema of the schema)."
    ),
    validators={"claude": _check_claude_deliver_tool, "grok": _check_grok_deliver_tool},
)


def _check_claude_native_format(kw: dict) -> None:
    fmt = (kw.get("output_config") or {}).get("format") or {}
    _eq(fmt.get("type"), "json_schema", "Claude: output_config.format.type must be json_schema")
    _true(all(t.get("name") != "deliver_response" for t in kw.get("tools") or []),
          "Claude: a request without client tools must not carry a deliver_response tool")


SCHEMA_WITHOUT_TOOLS_USES_NATIVE_FORMAT = ContractRule(
    name="SCHEMA_WITHOUT_TOOLS_USES_NATIVE_FORMAT",
    description=(
        "A dict response_schema without client tools keeps the native path. "
        "Claude: output_config.format.type == 'json_schema' and no deliver_response tool."
    ),
    validators={"claude": _check_claude_native_format},
)


# ---- Drive document reader (USER_DRIVE_INDEXING_RFC §24.2) -------------------------------------------------------

def _claude_document_blocks(kw: dict) -> list:
    """Every `document` block of the converted messages, including one nested in a tool_result."""
    blocks = []
    for msg in kw.get("messages") or []:
        for block in msg.get("content") if isinstance(msg.get("content"), list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "document":
                blocks.append(block)
            if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                blocks.extend(b for b in block["content"] if isinstance(b, dict) and b.get("type") == "document")
    return blocks


def _openai_input_files(kw: dict) -> list:
    return [part for item in kw.get("input") or [] if isinstance(item.get("content"), list)
            for part in item["content"] if isinstance(part, dict) and part.get("type") == "input_file"]


STRUCTURED_OUTPUT_PDF_SENDS_NO_CITATIONS = ContractRule(
    name="STRUCTURED_OUTPUT_PDF_SENDS_NO_CITATIONS",
    description=(
        "A PDF sent together with a response_schema: Anthropic rejects citations with output_config.format "
        "(HTTP 400), so no document block may enable them when the request carries output_config.format. "
        "Input: the captured messages.stream kwargs."
    ),
    validators={
        "claude": lambda kw: (
            _true("format" in (kw.get("output_config") or {}), "Claude: output_config.format missing"),
            _true(_claude_document_blocks(kw), "Claude: no document block was sent"),
            _true(all("citations" not in b for b in _claude_document_blocks(kw)),
                  "Claude: a document block enables citations next to output_config.format"),
        ),
    },
)

PDF_FILE_INPUT_IS_INLINE = ContractRule(
    name="PDF_FILE_INPUT_IS_INLINE",
    description=(
        "A PDF part goes inline with the request, never through a stored upload: OpenAI gets an input_file "
        "with a filename and file_data as a base64 data URL (no file_id, no Files API object left behind); "
        "Claude gets a document block with a base64 source. Input: the captured SDK call kwargs."
    ),
    validators={
        "openai": lambda kw: (
            _true(_openai_input_files(kw), "OpenAI: no input_file part was sent"),
            _true(all(p.get("filename") and str(p.get("file_data", "")).startswith("data:application/pdf;base64,")
                      and "file_id" not in p for p in _openai_input_files(kw)),
                  "OpenAI: input_file must carry filename + a base64 data URL and no file_id"),
        ),
        "claude": lambda kw: _true(
            _claude_document_blocks(kw) and all(b["source"]["type"] == "base64"
                                                 for b in _claude_document_blocks(kw)),
            "Claude: the PDF must be a document block with a base64 source",
        ),
    },
)


# ---- Drive agents' Gemini fallback (USER_DRIVE_INDEXING_RFC §24.2 D7 rev 3.17) -------------------------------------

def _gemini_inline_parts(kw: dict) -> list:
    """Every part of the converted contents that carries inline bytes."""
    return [part for content in kw.get("contents") or [] for part in getattr(content, "parts", None) or []
            if getattr(part, "inline_data", None) is not None]


INLINE_FILE_BYTES_ARE_SENT = ContractRule(
    name="INLINE_FILE_BYTES_ARE_SENT",
    description=(
        "A file part given as inline base64 (`file_data={'base64', 'mime_type'}` — the drive reader's PDF window or "
        "image, the triage's thumbnails) reaches the provider as the file's bytes, never dropped and never uploaded "
        "to a store. Gemini: one `inline_data` part per file, the decoded bytes and the mime type, and no Files API "
        "upload. Input: the captured generate_content kwargs plus `expected` — a list of (bytes, mime_type) in order."
    ),
    validators={
        "gemini": lambda kw: (
            _eq([(p.inline_data.data, p.inline_data.mime_type) for p in _gemini_inline_parts(kw)],
                kw["expected"], "Gemini: inline file parts differ from the files given"),
            _true(all(getattr(p, "file_data", None) is None for p in _gemini_inline_parts(kw)),
                  "Gemini: an inline file part also carries a file_data upload reference"),
        ),
    },
)

DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA = ContractRule(
    name="DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA",
    description=(
        "A dict `response_schema` with `response_mime_type='application/json'` (the drive agents' structured output) "
        "is enforced natively. Gemini: `config.response_json_schema` carries the schema (lowercase types; nullable "
        "as a type array, which Gemini's JSON Schema subset accepts), `config.response_schema` stays None, and "
        "`config.response_mime_type` is application/json. Input: the captured generate_content kwargs plus "
        "`expected_schema`."
    ),
    validators={
        "gemini": lambda kw: (
            _eq(kw["config"].response_json_schema, kw["expected_schema"],
                "Gemini: response_json_schema differs from the agent's schema"),
            _true(kw["config"].response_schema is None, "Gemini: a dict schema went to response_schema"),
            _eq(kw["config"].response_mime_type, "application/json", "Gemini: JSON mime type missing"),
        ),
    },
)


# ============================================================================
# Gemini batch adapter rules (USER_DRIVE_INDEXING_RFC §27.5, Δ131). Keyed by the adapter name "gemini_batch".
# Submit rules receive the kwargs `batches.create` was called with: {model, src: [InlinedRequest], config}.
# ============================================================================

def _each_request(kw: dict):
    reqs = kw["src"]
    assert reqs, "Gemini batch: no inline requests were sent"
    return reqs


def _check_schema_with_mime(kw: dict) -> None:
    for r in _each_request(kw):
        cfg = r.config
        _true(cfg.response_json_schema is None,
              "Gemini batch: response_json_schema is silently ignored in an inline request (Δ131)")
        if cfg.response_schema is None:
            continue  # a text-only request without a schema needs no mime type
        _eq(cfg.response_mime_type, "application/json",
            "Gemini batch: a schema needs response_mime_type application/json beside it")


def _check_thinking_low(kw: dict) -> None:
    for r in _each_request(kw):
        tc = r.config.thinking_config
        _true(tc is not None and tc.thinking_level == types.ThinkingLevel.LOW,
              f"Gemini batch: thinking_level LOW missing on request {r.metadata!r}")


def _check_key_round_trip(kw: dict) -> None:
    keys = [(r.metadata or {}).get("key") for r in _each_request(kw)]
    _true(all(keys), f"Gemini batch: every request must carry its key in metadata. Got: {keys!r}")
    _eq(len(set(keys)), len(keys), "Gemini batch: request keys must be unique")


def _check_file_uri_from_name(kw: dict) -> None:
    for r in _each_request(kw):
        for content in r.contents or []:
            for part in content.parts or []:
                if part.file_data is not None:
                    uri = part.file_data.file_uri or ""
                    _true("/files/" in uri and uri.rsplit("/files/", 1)[1],
                          "Gemini batch: file_uri must be built from the stored file name")
                    _true(part.file_data.mime_type, "Gemini batch: file part without mime type")


BATCH_SCHEMA_WITH_JSON_MIME = ContractRule(
    name="BATCH_SCHEMA_WITH_JSON_MIME",
    description=(
        "Every inline batch request that has a schema sends it as response_schema (typed) together with "
        "response_mime_type application/json, and never as response_json_schema, which a batch ignores silently."
    ),
    validators={"gemini_batch": _check_schema_with_mime},
)

BATCH_THINKING_LOW = ContractRule(
    name="BATCH_THINKING_LOW",
    description="Every inline batch request sets thinking_level LOW (thinking is on by default and billed as output).",
    validators={"gemini_batch": _check_thinking_low},
)

BATCH_KEY_ROUND_TRIP = ContractRule(
    name="BATCH_KEY_ROUND_TRIP",
    description="Every inline batch request carries a unique key in InlinedRequest.metadata; answers are matched by it.",
    validators={"gemini_batch": _check_key_round_trip},
)

BATCH_FILE_URI_FROM_STORED_NAME = ContractRule(
    name="BATCH_FILE_URI_FROM_STORED_NAME",
    description="A request's file part is a file_uri built from the stored provider file name, with its mime type.",
    validators={"gemini_batch": _check_file_uri_from_name},
)

BATCH_FIND_BY_SUBMITTED_DISPLAY_NAME = ContractRule(
    name="BATCH_FIND_BY_SUBMITTED_DISPLAY_NAME",
    description=(
        "find_batch(display_name) matches a listed job by exactly the display name submit() sent for the same argument "
        "(the adapter's hash), never by the raw argument (T-R28). Input: {argument, submitted: the display name "
        "batches.create received, matched: the display_name of the job find_batch returned}."
    ),
    validators={
        "gemini_batch": lambda d: (
            _eq(d["matched"], d["submitted"],
                "Gemini batch: find_batch must match the display name submit sent for the same argument"),
            _true(d["argument"] not in (d["submitted"] or ""),
                  "Gemini batch: the raw display name must never reach the provider"),
        ),
    },
)

BATCH_PER_RESPONSE_ERROR_SURFACED = ContractRule(
    name="BATCH_PER_RESPONSE_ERROR_SURFACED",
    description=(
        "A collected result whose own response held an error has an error, no text and the rpc code: a succeeded "
        "job with one failed response never yields a result for it. Input: {results: key -> BatchResult from "
        "collect(), failed_keys: the keys whose response held an error}; exactly those keys fail."
    ),
    validators={
        "gemini_batch": lambda d: (
            _eq({k for k, r in d["results"].items() if r.error}, set(d["failed_keys"]),
                "Gemini batch: the failed keys are not exactly those whose response held an error"),
            _true(all(d["results"][k].text is None and d["results"][k].code for k in d["failed_keys"]),
                  "Gemini batch: a per-response error result must have no text and carry its rpc code"),
        ),
    },
)

BATCH_THOUGHTS_IN_OUTPUT_LEG = ContractRule(
    name="BATCH_THOUGHTS_IN_OUTPUT_LEG",
    description=(
        "Thought tokens are billed as output: BatchUsage carries them and its cost equals the cost of "
        "candidates + thoughts output tokens. Input: {usage: BatchUsage, candidates: int, thoughts: int}."
    ),
    validators={
        "gemini_batch": lambda d: (
            _eq(d["usage"].output_tokens, d["candidates"], "Gemini batch: candidate tokens not the output leg"),
            _eq(d["usage"].thought_tokens, d["thoughts"], "Gemini batch: thought tokens missing from usage"),
            _true(
                abs(d["usage"].cost() - type(d["usage"])(
                    model=d["usage"].model, audio_input_tokens=d["usage"].audio_input_tokens,
                    text_input_tokens=d["usage"].text_input_tokens,
                    output_tokens=d["candidates"] + d["thoughts"]).cost()) < 1e-9,
                "Gemini batch: thought tokens not priced in the output leg",
            ),
        ),
    },
)


# ============================================================================
# Cloud KMS cipher (SECRETS_AT_REST_RFC §2.2, §8 Cipher): validator key "gcp_kms".
# Input: {"key_name": configured key, "aad": the CipherPort aad, "request": the request dict sent to
# KeyManagementServiceAsyncClient.encrypt or .decrypt}.
# ============================================================================

def _kms_crc32c(data: bytes) -> int:
    return int(google_crc32c.value(data))


def _kms_crc_fields(request: dict) -> None:
    data_field = "plaintext" if "plaintext" in request else "ciphertext"
    _true(data_field in request, "KMS: the request carries neither plaintext nor ciphertext")
    for name in (data_field, "additional_authenticated_data"):
        _eq(request.get(f"{name}_crc32c"), _kms_crc32c(request[name]),
            f"KMS: {name}_crc32c missing or not the CRC32C of {name}")


KMS_BINDS_KEY_AND_AAD = ContractRule(
    name="KMS_BINDS_KEY_AND_AAD",
    description=(
        "Every encrypt and decrypt request names the configured CryptoKey and carries the CipherPort aad, UTF-8, as "
        "additional_authenticated_data, so a ciphertext opens only under its own key and record (I2)."
    ),
    validators={
        "gcp_kms": lambda d: (
            _eq(d["request"].get("name"), d["key_name"], "KMS: request name is not the configured key"),
            _eq(d["request"].get("additional_authenticated_data"), d["aad"].encode("utf-8"),
                "KMS: additional_authenticated_data is not the aad bytes"),
        ),
    },
)

KMS_CRC32C_BOTH_WAYS = ContractRule(
    name="KMS_CRC32C_BOTH_WAYS",
    description=(
        "Every request carries the CRC32C of its data field (plaintext on encrypt, ciphertext on decrypt) and of its "
        "additional_authenticated_data, so KMS verifies what it received (Cloud KMS data-integrity guidelines)."
    ),
    validators={"gcp_kms": lambda d: _kms_crc_fields(d["request"])},
)
