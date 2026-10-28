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

from ..domain.drive_index_params import (
    PASSAGES_OUTPUT_TOKENS, PASSAGES_TIMEOUT_S, PROFILER_OUTPUT_TOKENS, PROFILER_TIMEOUT_S, TRIAGE_OUTPUT_TOKENS,
    TRIAGE_TIMEOUT_S,
)
from ..domain.user import PerformanceTier


# ========================================================================
# ARCHITECTURE FIX: Feature flags read from env ONCE at import time.
# Previously agents called os.getenv() inline — violates DI rule
# (agents must not read env vars directly). Now agents import these
# module-level constants instead of calling os.getenv() at runtime.
# ========================================================================
ENABLE_GROUNDING_ATTRIBUTION: bool = os.getenv(
    "ENABLE_GROUNDING_ATTRIBUTION", "false"
).lower() == "true"

DEEP_RESEARCH_SECOND_PASS: bool = os.getenv(
    "DEEP_RESEARCH_SECOND_PASS", "true"
).lower() != "false"


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
    # complexity_threshold / confidence_threshold removed: RFC TASK_COMPLEXITY_EXECUTION_SETTINGS.
    # Router no longer routes on numeric complexity — all user messages go to Smart; the LLM
    # classifies task_complexity (enum) and TaskExecutionResolver maps it to execution settings.


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
    # AgentConfig field — see RetryPolicy in domain/retry_policy.py for retry behavior.
    # 300 s: covers PDF attachment parsing via markitdown (confirmed >60 s in production)
    timeout_ms: int = 300_000
    # Dispatch-time intent substitution (applied in QuickResponseAgent._delegate_quick).
    intent_remap: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SmartResponseAgent (src/agents/core/smart_response_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class SmartAgentConfig:
    context_window: int = 30
    max_delegation_turns: int = 15
    max_agent_retries: int = 2
    retry_backoff_seconds: float = 1.0
    # 300 s: raised from 120 s to accommodate slower reasoning models (e.g. GPT-5)
    # NOTE: per-call timeout_ms is overridden via AgentMessage.timeout_ms by
    # UserNotificationService (NotificationKind/SLA) — see notification_sla.py.
    # Retry behavior lives in BaseAgent.RETRY_POLICY (RetryPolicy domain object).
    timeout_ms: int = 300_000
    # 64000: increased to accommodate 730-line system prompt + developer_message anchors
    # on OpenAI. Real prompts consume ~10k tokens before answer space.
    # Personality grounding via developer_message is load-bearing; truncation mid-response
    # destroys structured JSON and personality. Providers clamp to model max.
    max_tokens: int = 64_000


# ---------------------------------------------------------------------------
# FactsMemoryAgent (src/agents/memory_search_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class MemorySearchAgentConfig:
    max_tokens: int = 150
    result_limit: int = 10
    timeout_ms: int = 10_000


# ---------------------------------------------------------------------------
# WebSearchAgent (src/agents/web_search_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class WebSearchAgentConfig:
    """Behavior parameters for WebSearchAgent (two intents: search_web, fetch_url)."""

    timeout_ms: int = 90_000

    # Performance tier for the `fetch_url` intent ONLY. `search_web` always uses the
    # agent's resolved tier (UserBotConfig.get_tier_for_agent("web_search")).
    #
    # WHY A SEPARATE TIER: the two intents are different jobs. `search_web` is multi-angle
    # research — plan searches, weigh sources, reconcile contradictions. `fetch_url` opens
    # one known page and extracts the items named in the request. Measured 2026-07-29
    # (scripts/websearch/): on fetch_url the ECO model matched BALANCED on extracted-item
    # count at ~4.7x lower cost, while on search_web it lost ~25% of findings, ran 2x
    # slower, and dropped the required JSON shape on 1 of 7 real user queries.
    #
    # HOW IT IS APPLIED: this is a TIER, not a model. WebSearchAgent hands it to
    # LLMPort.get_model_for_tier(), so each provider maps it to its own cheap model
    # (OpenAI → gpt-6-luna, Gemini → flash-lite, Claude → haiku). The agent never names
    # a model — see the CLAUDE.md rule "the agent does not select the model itself".
    # Only OpenAI (the web_search default provider) was measured; the others are reached
    # on failover only.
    #
    # Set to None to disable the downgrade and run fetch_url on the agent's own tier.
    fetch_url_tier: Optional[PerformanceTier] = PerformanceTier.ECO


# ---------------------------------------------------------------------------
# ConsolidationAgent (src/agents/consolidation_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class ConsolidationAgentConfig:
    max_turns: int = 15          # max deliberation iterations; Stage 2 on large clusters (25+ facts) needs ~12 turns
    review_max_turns: int = 15   # the review pass's turn cap (Facts S4 §3.3); gate G5 wants every review pass within 13
    facts_limit: int = 50        # biographical cache limit passed at construction
    principles_limit: int = 15   # principles cache limit passed at construction
    # Output token limit. On Gemini 3 Pro, thinking_tokens count against
    # max_output_tokens — HIGH thinking on a complex consolidation turn can
    # consume 20K-30K of thoughts before any tool/text output appears.
    # 32K was tight; 64K leaves headroom for both thinking and the JSON report.
    # Claude doesn't approach this limit (~5-10K typical), so the bump is
    # Gemini-driven and safe across providers.
    max_tokens: int = 64_000
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
    # kick-off only — not the research execution time
    # Retry behavior: BaseAgent.RETRY_POLICY (default = retry transients).
    timeout_ms: int  = 30_000


# ---------------------------------------------------------------------------
# ClaudeDeepResearchRunnerAgent (src/agents/claude_deep_research_runner_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class ClaudeDeepResearchRunnerConfig:
    """
    Behavioral parameters for ClaudeDeepResearchRunnerAgent.

    timeout_ms covers the full research execution (up to 30 min per Cloud Task deadline).
    Retry: ClaudeDeepResearchRunnerAgent overrides RETRY_POLICY = NO_RETRY_POLICY
    because a retry doubles wall time (10–25 min) and adds significant cost.
    """
    timeout_ms: int = 1_800_000  # 30 min — used only when invoked via coordinator (legacy path)
    #                              In job_main.py the agent is created with timeout_ms=None
    #                              so the Cloud Run Job task-timeout is the ceiling.


# ---------------------------------------------------------------------------
# ComputeAgent (src/agents/compute_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class ComputeAgentConfig:
    timeout_ms: int = 30_000     # single code_execution call


# ---------------------------------------------------------------------------
# WebSearchLightAgent (src/agents/web_search_light_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class WebSearchLightAgentConfig:
    # One grounded call, ~2 s measured; the ceiling only guards a hung provider on a live phone call.
    timeout_ms: int = 20_000


# ---------------------------------------------------------------------------
# MapsSearchAgent (src/agents/maps_search_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class MapsSearchAgentConfig:
    # MCP-backed agent (Google Maps AI Grounding Lite).
    # Provider resolved via AgentProviderStrategy — no model pin needed.
    # Pricing: free during experimental quota phase.
    # Timeout: allows for multi-turn tool loop (up to 4 turns).
    # thinking="low" is the dominant latency lever: every allowed provider
    # (Gemini 3, Claude, gpt-5) reasons at its model default when thinking is
    # unset. Omitting it made the multi-turn synthesis call run 26-52s. "low"
    # forces the minimum reasoning level (the floor on Gemini 3 / gpt-5 — full
    # off is not available on those families). Provider-agnostic: maps to
    # ThinkingLevel.LOW (Gemini) / reasoning.effort="low" (OpenAI).
    thinking: str = "low"
    timeout_ms: int = 90_000


# ---------------------------------------------------------------------------
# TutorExtractorAgent (src/agents/tutor_extractor_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class TutorExtractorAgentConfig:
    # Single-shot structured JSON call, no multi-turn tool loop — much shorter
    # budget than ConsolidationAgent's 15-min 8-step loop.
    timeout_ms: int = 300_000  # 5 min
    max_tokens: int = 4096


# ---------------------------------------------------------------------------
# LelikSummarizerAgent (src/agents/lelik_summarizer_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class VoiceSummarizerAgentConfig:
    # Single-shot plain-text summary call over one call's turns — no structured
    # JSON, no records (Lelik has no CompanionRecord store, RFC §4.9), so this
    # is far cheaper than TutorExtractorAgentConfig's 5-min JSON-extraction
    # budget. A phone call's transcript is bounded by call duration, not an
    # accumulating batch window like the tutor's. Confirm this value against
    # real turn counts during Task 20's manual verification.
    timeout_ms: int = 60_000  # 1 min
    max_tokens: int = 2048


# ---------------------------------------------------------------------------
# DriveDocumentReaderAgent / DriveImageTriageAgent (src/agents/drive_*_agent.py), indexer only
# (USER_DRIVE_INDEXING_RFC §24.2). One call per PDF window or thumbnail batch; no history, no tools.
# ---------------------------------------------------------------------------

@dataclass
class DriveReaderAgentConfig:
    # One window of MODEL_PAGES_PER_CALL pages: ≈ 60 s a round (§24.13). A call past this halves a multi-page window
    # once; a half past it again fails the file (`error`, a counted per-file retry, T-M2b); a single-page window
    # past it, never halved, is a model outage (§24.3). Calibrate with the live model check's timing
    # summary (scripts/validation/drive_model_check.py: slowest call, output tokens/s).
    call_timeout_s: int = 180
    # ≈ 8k output tokens for ten dense pages, plus low-effort thinking, which counts toward the cap on Claude.
    max_tokens: int = 16_000
    max_parse_retries: int = 1
    # Transcription needs no deliberation. Haiku 5.5 takes adaptive thinking + effort (it 400s on
    # thinking.type=enabled); gpt-6-luna reasons by default, so "low" bounds it. A user's agent_thinking wins.
    thinking: str = "low"


@dataclass
class DriveTriageAgentConfig:
    call_timeout_s: int = TRIAGE_TIMEOUT_S
    # Triage v2 (USER_DRIVE_INDEXING_RFC §25.4.3, §25.4.4): TRIAGE_BATCH photo descriptions (description, objects with
    # forms, scene, visible text) — the schema's worst case plus a reasoning allowance.
    max_tokens: int = TRIAGE_OUTPUT_TOKENS
    max_parse_retries: int = 1
    thinking: str = "low"


# DriveProfilerAgent / DrivePassageAgent (Search v2, USER_DRIVE_INDEXING_RFC §25.4.1, §25.4.2, §25.4.4): one call per
# file profile or per PASSAGE_CHUNKS_PER_CALL chunks; the caps are the schemas' worst case plus a "low" reasoning
# allowance (unit-tested); a reply stopped at the cap is halved by the runner.
@dataclass
class DriveProfilerAgentConfig:
    call_timeout_s: int = PROFILER_TIMEOUT_S
    max_tokens: int = PROFILER_OUTPUT_TOKENS
    max_parse_retries: int = 1
    thinking: str = "low"


@dataclass
class DriveTranscriberAgentConfig:
    """Batch transcription of one audio window (USER_DRIVE_INDEXING_RFC §27.5). Not a `_call_llm` agent: no timeout or
    parse retry here (a batch is hours; the stage resubmits). `max_tokens` is the provider's output ceiling; each
    window's own cap is min(this, ceil(1000 x seconds / 60)) (T-R19). T0 measured about 400-450 output tokens per
    audio minute, and the thought tokens count toward the cap."""
    max_tokens: int = 65_536
    thinking: str = "low"


@dataclass
class DrivePassageAgentConfig:
    call_timeout_s: int = PASSAGES_TIMEOUT_S
    max_tokens: int = PASSAGES_OUTPUT_TOKENS
    max_parse_retries: int = 1
    thinking: str = "low"


# ---------------------------------------------------------------------------
# Module-level instances — agents import and reference these at class-definition time
# ---------------------------------------------------------------------------

BASE = BaseAgentConfig()
ROUTER = RouterAgentConfig()
QUICK = QuickAgentConfig()
SMART = SmartAgentConfig()
MEMORY_SEARCH = MemorySearchAgentConfig()
WEB_SEARCH = WebSearchAgentConfig()
CONSOLIDATION = ConsolidationAgentConfig()
TUTOR_EXTRACTOR = TutorExtractorAgentConfig()
VOICE_SUMMARIZER = VoiceSummarizerAgentConfig()
EMAIL_SEARCH = EmailSearchAgentConfig()
EMAIL_CLASSIFICATION = EmailClassificationAgentConfig()
DEEP_RESEARCH = DeepResearchAgentConfig()
CLAUDE_DEEP_RESEARCH_RUNNER = ClaudeDeepResearchRunnerConfig()
MAPS_SEARCH = MapsSearchAgentConfig()
COMPUTE = ComputeAgentConfig()
WEB_SEARCH_LIGHT = WebSearchLightAgentConfig()
DRIVE_READER = DriveReaderAgentConfig()
DRIVE_TRIAGE = DriveTriageAgentConfig()
DRIVE_PROFILER = DriveProfilerAgentConfig()
DRIVE_PASSAGES = DrivePassageAgentConfig()
DRIVE_TRANSCRIBER = DriveTranscriberAgentConfig()


# ---------------------------------------------------------------------------
# NotesAgent (src/agents/notes_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class NotesAgentConfig:
    max_tokens: int = 4096        # Reasoning models eat budget before output; CRUD tool args
                                  # (text + instruction + due + recurrence) need headroom or
                                  # OpenAI truncates JSON mid-string → JSONDecodeError → args={}
    max_turns: int = 3            # delegation → compute → CRUD
    timeout_ms: int = 30_000      # Multi-turn with chain delegation


NOTES = NotesAgentConfig()


# ---------------------------------------------------------------------------
# TasksAgent (src/agents/tasks_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class TasksAgentConfig:
    max_tokens: int = 1024        # Tool-calling loop: tool calls + final text response
    timeout_ms: int = 30_000      # Multi-turn: up to 2 tool calls + final synthesis


TASKS = TasksAgentConfig()


# ---------------------------------------------------------------------------
# DriveAgent (src/agents/drive_agent.py) — the drive specialist (DRIVE_SPECIALIST_RFC §5.1, §5.6)
# ---------------------------------------------------------------------------

@dataclass
class DriveAgentConfig:
    max_tokens: int = 4096            # tool calls and a short report; thinking counts toward it on Claude
    max_turns: int = 6                # one commission: read, read, write, answer (§2.3.7)
    # The hard timeout around execute() is a backstop only: the soft deadline ends every run first and returns the
    # answer from the collector (§5.6).
    timeout_ms: int = 180_000
    soft_deadline_s: float = 150.0
    turn_clock_margin_s: float = 15.0   # under the caller's turn clock: now + clock.call_timeout() − this
    run_deadline_margin_s: float = 20.0  # under the caller's own timeout (CURRENT_RUN_DEADLINE) − this
    call_timeout_s: int = 120         # one LLM call at most; clamped to the time left (§5.6)
    # claude-sonnet-5-5 adaptive thinking, effort medium (owner decision 2026-10-22). A user's agent_thinking wins.
    thinking_effort: str = "medium"


DRIVE = DriveAgentConfig()


# ---------------------------------------------------------------------------
# DocPlannerAgent (src/agents/doc_planner_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class DocPlannerAgentConfig:
    max_tokens: int = 54_000      # JSON spec for a full document can be large
    timeout_ms: int = 1_100_000   # Background async task — allow ~18 min for spec generation
    thinking_effort: Optional[str] = "high"
    # Per-LLM-request timeout (seconds), passed as LLMRequest.timeout. Heavy
    # layout-spec generation on reasoning models can run several minutes, exceeding the
    # OpenAI client's 300s default; set below timeout_ms so the request completes before
    # the Cloud Task deadline rather than being clamped/retried. Raised 540s→900s 2026-10-07
    # alongside HtmlPageGenerator/PdfGenerator — same owner-accepted ASYNC ceiling, see
    # decisions/html_page_fallback_context_wiring.md.
    request_timeout_s: int = 900


DOC_PLANNER = DocPlannerAgentConfig()


# ---------------------------------------------------------------------------
# DocGeneratorAgent (src/agents/doc_generator_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class DocGeneratorAgentConfig:
    max_tokens: int = 64_000      # Full Node.js script can be large
    # Background async task — allow ~18 min total across the MAX_TURNS=5 tool-calling loop.
    # Raised 600s→1100s 2026-10-07 alongside the other ASYNC generators (see
    # decisions/html_page_fallback_context_wiring.md). No per-call request_timeout_s here:
    # unlike HtmlPageGenerator/PdfGenerator/DocPlanner this agent is a multi-turn loop, not a
    # single LLM call, so a flat per-call budget doesn't transfer — each turn still falls back
    # to the provider SDK's own default (e.g. Claude ~120s), bounded in aggregate by timeout_ms.
    timeout_ms: int = 1_100_000
    node_timeout_s: int = 60      # Subprocess timeout
    thinking_effort: Optional[str] = None


DOC_GENERATOR = DocGeneratorAgentConfig()


# ---------------------------------------------------------------------------
# PdfGeneratorAgent (src/agents/pdf_generator_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class PdfGeneratorAgentConfig:
    max_tokens: int = 64_000      # Full HTML+CSS document can be large
    timeout_ms: int = 1_100_000   # Background async task — allow ~18 min for generation
    node_timeout_s: int = 60  # Subprocess timeout
    thinking_effort: Optional[str] = None
    # Per-LLM-request timeout (seconds), passed as LLMRequest.timeout. Previously unset (SDK
    # default only — e.g. OpenAI's 300s client ceiling), which on a slow turn risks the SDK's
    # own internal retries silently paying for multiple generations before timeout_ms kills the
    # agent anyway (the same waste HtmlPageGenerator's ceiling exists to avoid). Added 2026-10-07
    # alongside the other single-call ASYNC generators, see
    # decisions/html_page_fallback_context_wiring.md.
    request_timeout_s: int = 900


PDF_GENERATOR = PdfGeneratorAgentConfig()


# ---------------------------------------------------------------------------
# HtmlPageGeneratorAgent (src/agents/html_page_generator_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class HtmlPageGeneratorAgentConfig:
    max_tokens: int = 64_000      # Full HTML+CSS+JS document can be large
    timeout_ms: int = 1_100_000   # Background async task — allow ~18 min for generation
    thinking_effort: Optional[str] = "medium"
    # Per-LLM-request timeout (seconds), passed as LLMRequest.timeout. Bounds TOTAL
    # wall time including any SDK retries (adapters wrap the call in asyncio.wait_for),
    # which is the point: without it a call that overruns the provider's client ceiling
    # is retried twice more, and three generations are paid for and thrown away before
    # timeout_ms kills the agent anyway. Originally 420s (measured 2026-08-15: grok-4.6
    # took 229s, gemini-pro-latest 120s). Raised to 900s on 2026-10-07 after grok-4.7
    # (default since 2026-10-03) overran 420s on a live request and killed the whole
    # generation with no fallback (see decisions/html_page_fallback_context_wiring.md —
    # the Gemini fallback was also dead at the time, fixed in the same incident). 900s
    # is an owner-accepted ceiling for an ASYNC intent nobody blocks on; timeout_ms keeps
    # ~200s of headroom above it for Unsplash resolution + GCS upload.
    request_timeout_s: int = 900


HTML_PAGE_GENERATOR = HtmlPageGeneratorAgentConfig()


# ---------------------------------------------------------------------------
# DomainResearcherAgent (src/agents/domain_researcher_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class DomainResearcherAgentConfig:
    max_tokens: int = 32_000      # Complex analytical output — domain decomposition + scoring
    timeout_ms: int = 120_000     # Reasoning models take longer (high effort)
    context_window: int = 30      # Bound channel history messages to fetch
    thinking_effort: Optional[str] = "high"  # Deep analytical reasoning
    max_delegation_turns: int = 5  # Multi-turn tool calling loop


DOMAIN_RESEARCHER = DomainResearcherAgentConfig()


# ---------------------------------------------------------------------------
# TutorAgent (src/agents/tutor_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class TutorAgentConfig:
    # 32768: same order of magnitude as DomainResearcherAgentConfig (32_000), this agent's
    # own structural template. Phase G gave Tutor a JSON-schema output contract
    # (full_response/response_summary) — truncation mid-response delivers malformed/raw
    # JSON to the student, a worse failure mode than a merely-cut-off sentence.
    max_tokens: int = 32_768
    # 120 s: matches DomainResearcherAgentConfig.timeout_ms, the template this agent is
    # based on (same max_delegation_turns=5 budget). 60s was too tight — WebSearchAgent's
    # own timeout_ms (90_000, search_web is in allowed_intents) can outlive the tutor's
    # entire execution budget, since BaseAgent._execute_with_timeout wraps the whole
    # execute() call — DelegationEngine loop included — in this timeout.
    timeout_ms: int = 120_000
    max_delegation_turns: int = 5  # matches DomainResearcher's multi-turn tool loop budget


TUTOR = TutorAgentConfig()


# ---------------------------------------------------------------------------
# ImageGenerationAgent (src/agents/image_generation_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class ImageGenerationAgentConfig:
    max_tokens: int = 2_000       # Output is a single prompt string, not a document
    # Hard wall-clock budget around the entire execute() call (prompt-builder read +
    # LLM prompt-crafting call + image render + encoding). Deliberately BELOW
    # AgentDescriptor.dispatch_deadline_s=520 (agent_manifest.py IMAGE_GENERATION) —
    # a 120s gap, same "agent timeout + 2 min overhead" slack every sibling async
    # agent keeps (see HTML_PAGE_GENERATOR etc.). This lets the agent's own timeout
    # fire and go through the normal failure-notification path (AgentWorkerHandler
    # -> _notify_docx_failure) BEFORE Cloud Tasks kills the task externally —
    # an external kill triggers a Cloud Tasks retry, re-running (and re-billing)
    # the xAI call. Do NOT set this equal to dispatch_deadline_s.
    # 400_000 = request_timeout_s (60s, prompt-crafting) + GrokImageAdapter's SDK
    # timeout (300s, image render, raised 2026-08-21 after a real generate() call
    # exceeded the previous 120s) + ~40s margin for encoding/GCS upload/network
    # jitter. Still not a fully measured ceiling (RFC §10 open question #3), but
    # sized from one real production timeout instead of a guess.
    timeout_ms: int = 400_000
    thinking_effort: Optional[str] = None
    # Per-LLM-request timeout (seconds) for the prompt-crafting call, passed as
    # LLMRequest.timeout. Conservative starting value — no latency data yet for
    # grok-imagine-image-2.0 (RFC §10 open question #3). Do NOT copy
    # HTML_PAGE_GENERATOR's 420s — that's sized for long text/HTML generation,
    # not a short prompt-translation call. Revisit after first live measurements.
    request_timeout_s: int = 60


IMAGE_GENERATION = ImageGenerationAgentConfig()


# ---------------------------------------------------------------------------
# VideoGenerationAgent (src/agents/video_generation_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class VideoGenerationAgentConfig:
    # Output is {video_prompt, aspect_ratio} JSON, but a detailed second-by-second brief
    # yields a long video_prompt. 500 truncated one mid-string on 2026-10-10 (grok-4.7,
    # ~2.4k chars visible) — the request failed as "invalid JSON". Sized for long briefs.
    max_tokens: int = 16_000
    # Hard wall-clock budget around the entire execute() call (prompt-builder read +
    # LLM crafting call + xAI submit-only POST). Unlike ImageGenerationAgentConfig,
    # this does NOT need to cover a full render — VideoGenerationAgent is
    # ExecutionMode.SYNC and returns an ACK once submitted; the multi-minute wait lives
    # entirely in WorkerHandler's polling loop, outside this budget. Sized as
    # request_timeout_s (crafting call) + GrokVideoAdapter's submit timeout (60s) +
    # margin.
    timeout_ms: int = 900_000
    # Per-LLM-request timeout (seconds) for the crafting call. The PERFORMANCE-tier
    # crafting model reasons before answering; on a ~1.5k-token brief (2026-10-10,
    # grok-4.7) it took over 60s and the request failed with LLMTimeoutError.
    request_timeout_s: int = 600


VIDEO_GENERATION = VideoGenerationAgentConfig()
