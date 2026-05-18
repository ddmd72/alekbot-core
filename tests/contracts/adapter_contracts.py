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
