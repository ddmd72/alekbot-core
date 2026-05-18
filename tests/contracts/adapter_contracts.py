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
