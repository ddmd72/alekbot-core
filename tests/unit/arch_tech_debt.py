"""
Architectural Tech Debt Registry
=================================

Known architectural violations that exist in the codebase and have not yet been fixed.
Each entry is consumed by test_req_arch_01_hexagonal_isolation.py — if a violation is
fixed in production code, remove the corresponding entry here. The test will confirm
the fix by passing without the whitelist entry.

PROCESS
-------
  Adding an entry   — requires sign-off from codebase owner; must describe the fix.
  Removing an entry — means the violation has been FIXED in code (not just deleted here).

DO NOT add entries to avoid a failing test. Fix the code instead.

Added: 2026-03-08
"""

import os

# ---------------------------------------------------------------------------
# TD-V1 — FIXED (2026-03-08)
# LLM models moved to src/domain/llm.py; ports now import from domain.
# ---------------------------------------------------------------------------
CROSS_PORT_WHITELIST: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# TD-V2 — Pydantic BaseModel subclasses in ports/
#
# Root cause: LLM and auth data models were placed in ports/ for convenience
# but belong in domain/ (pure data, zero external dependencies).
#
# Fix: create src/domain/llm.py (LLM models) and src/domain/auth.py (auth DTOs),
# migrate all classes below there, update importers. Remove entries below.
# ---------------------------------------------------------------------------
PORT_DATA_MODELS_WHITELIST: set[str] = {
    # AgentExecutionContext intentionally stays in ports/llm_port.py:
    # it holds a runtime LLMPort reference — moving to domain/ would create domain→ports dependency.
    "AgentExecutionContext",
    # LLM models — FIXED 2026-03-08: moved to src/domain/llm.py
    # Auth DTOs  — FIXED 2026-03-08: moved to src/domain/auth.py
}


# ---------------------------------------------------------------------------
# TD-V3 — FIXED (2026-03-08)
# SecurityPort moved to src/ports/security_port.py.
# ---------------------------------------------------------------------------
PORT_IN_DOMAIN_WHITELIST: set[str] = set()


# ---------------------------------------------------------------------------
# TD-V4 — Cross-subpackage adapter imports
#
# Root cause: several adapters import concrete implementations from other
# adapter sub-packages, coupling the delivery layer internally.
#
# Format: (normalized_file_path, resolved_absolute_module)
# ---------------------------------------------------------------------------
CROSS_ADAPTER_WHITELIST: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# TD-V5 — Model name strings in intentionally provider-specific agents
#
# These files are allowed to contain concrete model name strings (e.g.
# "claude-sonnet-4-6") because they are by design bound to a single provider.
# Consumed by REQ-ARCH-12.
#
# Format: normalized file path strings.
# ---------------------------------------------------------------------------
MODEL_NAME_WHITELIST_FILES: set[str] = {
    # Pricing lookup table — model names are data, not logic.
    os.path.normpath("src/domain/billing.py"),
    # Intentionally Claude-specific agent (extended thinking, Claude-only capability).
    # _THINKING_MODELS set is adapter-internal knowledge embedded by design.
    os.path.normpath("src/agents/claude_deep_research_runner_agent.py"),
}


# ---------------------------------------------------------------------------
# TD-V6 — HTTP client libraries in services/
#
# Root cause: GmailOAuthService makes direct HTTP calls to Google OAuth2
# endpoints (token exchange, userinfo). This is a thin wrapper, not a
# general pattern — it's a single implementation called only by web endpoints.
#
# Fix: extract into an OAuthHttpPort + GoogleOAuthAdapter if a second
# OAuth provider is ever added. Until then, whitelisting is pragmatic.
#
# Format: normalized file path strings.
# Consumed by REQ-ARCH-17.
# ---------------------------------------------------------------------------
HTTP_CLIENT_WHITELIST_FILES: set[str] = {
    os.path.normpath("src/services/google_oauth_service.py"),
}


# ---------------------------------------------------------------------------
# TD-V8 — Handler implementing its own primary port
#
# Root cause: ConversationHandler implements ConversationHandlerPort, which is
# a primary (driving) port — a contract for the delivery layer (Slack, Telegram)
# to call into the application core. The handler must import the port to declare
# its conformance. This is a valid hexagonal pattern, not a violation.
#
# No fix needed. Do not remove unless the pattern itself is eliminated.
#
# Format: (normalized_file_path, resolved_absolute_module)
# Consumed by REQ-ARCH-25.
# ---------------------------------------------------------------------------
HANDLER_IMPLEMENTS_PORT_WHITELIST: set[tuple[str, str]] = {
    (
        "src/handlers/conversation_handler.py",
        "src.ports.conversation_handler_port",
    ),
}


# ---------------------------------------------------------------------------
# TD-V7 — Platform-specific formatting references in agents/services
#
# Root cause: fallback system prompts in web search agents contain
# "Slack mrkdwn" formatting instructions. These are used only when the
# prompt builder fails to load the real prompt from the database.
# prompt_builder.py contains Slack_Formatting_Protocol as part of the
# prompt assembly pipeline (platform-aware by design).
#
# Fix: make fallback prompts platform-agnostic by injecting formatting
# rules at assembly time (same as the normal prompt path does).
#
# Format: normalized file path strings.
# Consumed by REQ-ARCH-20.
# ---------------------------------------------------------------------------
PLATFORM_FORMAT_WHITELIST_FILES: set[str] = {
    # Fallback system prompts contain "Slack mrkdwn" instructions.
    os.path.normpath("src/agents/web_search_agent.py"),
    os.path.normpath("src/agents/web_search_light_agent.py"),
    # Prompt builder assembles platform-specific formatting rules by design.
    os.path.normpath("src/services/prompt_builder.py"),
}
