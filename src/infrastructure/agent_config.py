"""
Agent Runtime Configuration
============================

Central registry of tunable BEHAVIOR parameters for all agents.
Edit values here — agents reference these at class-definition time.

PROVIDER CONFIGURATION IS SEPARATE:
    Default provider, allowed providers, required capabilities, and fallback chain
    are defined per-agent-type in AgentProviderStrategy:
        → src/services/agent_context_builder.py :: AgentProviderStrategy.STRATEGIES

NOTE on BaseAgent.HISTORY_FULL_TURNS:
    BaseAgent cannot import from infrastructure/ (circular import:
    infrastructure/__init__.py → agent_coordinator.py → agents/__init__.py → base_agent.py).
    HISTORY_FULL_TURNS in base_agent.py is assigned as a literal with a comment pointing here.
    Keep BaseAgentConfig.history_full_turns in sync manually.

Extension path (Level 2 — dynamic overrides):
    To support per-user/per-account Firestore overrides without touching agent code,
    replace class-level constant assignments with constructor-injected instance attributes
    and introduce a get_agent_config(agent_type, user_id) function backed by an
    AgentConfigPort adapter that checks Firestore before returning static defaults.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Optional


# ========================================================================
# ARCHITECTURE FIX: Feature flags read from env ONCE at import time.
# Previously agents called os.getenv() inline — violates DI rule
# (agents must not read env vars directly). Now agents import these
# module-level constants instead of calling os.getenv() at runtime.
# ========================================================================
ENABLE_HISTORY_OPTIMIZATION: bool = os.getenv(
    "ENABLE_HISTORY_OPTIMIZATION", "false"
).lower() in ("true", "1", "yes")

ENABLE_GROUNDING_ATTRIBUTION: bool = os.getenv(
    "ENABLE_GROUNDING_ATTRIBUTION", "false"
).lower() == "true"


# ---------------------------------------------------------------------------
# BaseAgent (src/agents/base_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class BaseAgentConfig:
    # Most-recent model turns to keep at full text (not summarised).
    # !! Must stay in sync with BaseAgent.HISTORY_FULL_TURNS literal (see NOTE above).
    history_full_turns: int = 5


# ---------------------------------------------------------------------------
# RouterAgent (src/agents/core/router_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class RouterAgentConfig:
    # History turns passed to the triage LLM call
    context_window: int = 5
    # Biographical context fetch limit
    biographical_limit: int = 100
    # Routing thresholds: complexity <= threshold → Quick; > threshold → Smart
    complexity_threshold: int = 6
    # Routing safety net: confidence < threshold → always fall back to Smart
    confidence_threshold: float = 0.75


# ---------------------------------------------------------------------------
# QuickResponseAgent (src/agents/core/quick_response_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class QuickAgentConfig:
    # LLM history window (smaller = faster + cheaper)
    context_window: int = 30
    # Specialist delegation loop limit
    max_delegation_turns: int = 8
    # Retries when a specialist agent call fails
    max_agent_retries: int = 1
    retry_backoff_seconds: float = 0.5
    # LLM temperature for the delegation loop
    delegation_temperature: float = 0.9
    # AgentConfig fields (timeout / outer retry for the whole agent execution)
    # 300 s: covers PDF attachment parsing via markitdown (confirmed >60 s in production)
    timeout_ms: int = 300_000
    config_max_retries: int = 1
    # Dispatch-time intent substitution (applied in QuickResponseAgent._delegate_quick).
    # search_web → search_web_light: Quick routes web queries via the cheaper ECO-tier agent.
    intent_remap: Dict[str, str] = field(
        default_factory=lambda: {"search_web": "search_web_light"}
    )


# ---------------------------------------------------------------------------
# SmartResponseAgent (src/agents/core/smart_response_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class SmartAgentConfig:
    context_window: int = 30
    max_delegation_turns: int = 8
    max_agent_retries: int = 2
    retry_backoff_seconds: float = 1.0
    delegation_temperature: float = 0.8
    # 300 s: matches Quick ceiling; Cloud Run allows up to 3600 s
    timeout_ms: int = 300_000
    # No retry: a retry doubles wall time to ~5 min → terrible UX
    config_max_retries: int = 0


# ---------------------------------------------------------------------------
# MemorySearchAgent (src/agents/memory_search_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class MemorySearchAgentConfig:
    temperature: float = 0.0     # deterministic key extraction
    max_tokens: int = 150
    result_limit: int = 10
    timeout_ms: int = 10_000


# ---------------------------------------------------------------------------
# WebSearchAgent (src/agents/web_search_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class WebSearchAgentConfig:
    temperature: float = 0.5
    timeout_ms: int = 90_000


# ---------------------------------------------------------------------------
# WebSearchLightAgent (src/agents/web_search_light_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class WebSearchLightAgentConfig:
    temperature: float = 0.5     # lighter than full web search
    timeout_ms: int = 30_000


# ---------------------------------------------------------------------------
# ConsolidationAgent (src/agents/consolidation_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class ConsolidationAgentConfig:
    max_turns: int = 15          # max deliberation iterations; Stage 2 on large clusters (25+ facts) needs ~12 turns
    temperature: float = 0.0
    facts_limit: int = 50        # biographical cache limit passed at construction
    principles_limit: int = 15   # principles cache limit passed at construction
    max_tokens: int = 32_000         # output token limit; large enough for full fact JSON reports
    # Thinking effort: None | "low" | "medium" | "high". None = thinking disabled.
    # Adapters gate thinking on this value — None skips the thinking block entirely.
    thinking_effort: Optional[str] = "medium"
    # Inline cluster review (Stage 2) after Stage 1 in the overflow flow.
    # Set to False to disable and rely on the scheduled ClusterReviewService only.
    inline_cluster_review: bool = True
    # Email triage (Stage 3) defaults — used by _handle_consolidate_email when
    # number_of_batches / batch_size are absent from payload.
    email_triage_passes: int = 1
    email_triage_batch_size: int = 100
    # 15 min: covers consolidate_full (Stage 1 ~4 min + Stage 2 ~3 min + Stage 3 ~4 min).
    # Cloud Tasks consolidation tasks should have matching or higher deadline.
    timeout_ms: int = 900_000


# ---------------------------------------------------------------------------
# EmailSearchAgent (src/agents/email_search_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class EmailSearchAgentConfig:
    temperature: float = 0.0     # deterministic key extraction
    max_tokens: int = 250
    # 300 s: PDF attachment parsing via markitdown can be slow (confirmed >30 s in production)
    timeout_ms: int = 300_000


# ---------------------------------------------------------------------------
# EmailClassificationAgent (src/agents/email_classification_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class EmailClassificationAgentConfig:
    max_turns: int = 4           # matches POC; LLM may call get_email_details() mid-loop
    max_parse_retries: int = 1   # one LLM retry on invalid JSON before giving up
    temperature: float = 0.0     # deterministic classification
    max_tokens: int = 65_535     # near Gemini limit; reasoning mode needs headroom


# ---------------------------------------------------------------------------
# DeepResearchAgent (src/agents/deep_research_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class DeepResearchAgentConfig:
    """
    Behavioral parameters for DeepResearchAgent.

    timeout_ms covers the create_interaction() kick-off call only (returns quickly).
    The 5–60 minute research execution runs via Cloud Tasks polling, not within this timeout.
    """
    timeout_ms: int  = 30_000   # kick-off only — not the research execution time
    max_retries: int = 2


# ---------------------------------------------------------------------------
# ClaudeDeepResearchRunnerAgent (src/agents/claude_deep_research_runner_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class ClaudeDeepResearchRunnerConfig:
    """
    Behavioral parameters for ClaudeDeepResearchRunnerAgent.

    timeout_ms covers the full research execution (up to 30 min per Cloud Task deadline).
    max_retries=0 because a retry doubles wall time (10–25 min) and adds significant cost.
    """
    timeout_ms: int = 1_800_000  # 30 min — matches Cloud Task dispatch_deadline
    max_retries: int = 0


# ---------------------------------------------------------------------------
# ComputeAgent (src/agents/compute_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class ComputeAgentConfig:
    temperature: float = 0.0     # deterministic computation
    timeout_ms: int = 30_000     # single code_execution call, same ceiling as WebSearchLight


# ---------------------------------------------------------------------------
# MapsSearchAgent (src/agents/maps_search_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class MapsSearchAgentConfig:
    # MCP-backed agent (Google Maps AI Grounding Lite).
    # Provider resolved via AgentProviderStrategy — no model pin needed.
    # Pricing: free during experimental quota phase.
    # Timeout: allows for multi-turn tool loop (up to 4 turns).
    temperature: float = 0.3
    timeout_ms: int = 90_000


# ---------------------------------------------------------------------------
# Module-level instances — agents import and reference these at class-definition time
# ---------------------------------------------------------------------------

BASE = BaseAgentConfig()
ROUTER = RouterAgentConfig()
QUICK = QuickAgentConfig()
SMART = SmartAgentConfig()
MEMORY_SEARCH = MemorySearchAgentConfig()
WEB_SEARCH = WebSearchAgentConfig()
WEB_SEARCH_LIGHT = WebSearchLightAgentConfig()
CONSOLIDATION = ConsolidationAgentConfig()
EMAIL_SEARCH = EmailSearchAgentConfig()
EMAIL_CLASSIFICATION = EmailClassificationAgentConfig()
DEEP_RESEARCH = DeepResearchAgentConfig()
CLAUDE_DEEP_RESEARCH_RUNNER = ClaudeDeepResearchRunnerConfig()
MAPS_SEARCH = MapsSearchAgentConfig()
COMPUTE = ComputeAgentConfig()


# ---------------------------------------------------------------------------
# TasksAgent (src/agents/tasks_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class TasksAgentConfig:
    temperature: float = 0.3      # Low: structured operations, not creative
    max_tokens: int = 1024        # Tool-calling loop: tool calls + final text response
    timeout_ms: int = 30_000      # Multi-turn: up to 2 tool calls + final synthesis


TASKS = TasksAgentConfig()


# ---------------------------------------------------------------------------
# DocPlannerAgent (src/agents/doc_planner_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class DocPlannerAgentConfig:
    temperature: float = 1.0      # Claude default for JSON generation without thinking
    max_tokens: int = 54_000      # JSON spec for a full document can be large
    timeout_ms: int = 600_000     # Background async task — allow 10 min for spec generation
    thinking_effort: Optional[str] = None


DOC_PLANNER = DocPlannerAgentConfig()


# ---------------------------------------------------------------------------
# DocGeneratorAgent (src/agents/doc_generator_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class DocGeneratorAgentConfig:
    temperature: float = 0.5      # Balanced: code precision + rendering simulation reasoning
    max_tokens: int = 64_000      # Full Node.js script can be large
    timeout_ms: int = 600_000     # Background async task — allow 10 min for code generation
    node_timeout_s: int = 60      # Subprocess timeout
    thinking_effort: Optional[str] = None


DOC_GENERATOR = DocGeneratorAgentConfig()
