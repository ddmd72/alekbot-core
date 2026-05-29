# RFC: Deep Research Integration

**Status:** Gemini backend implemented ✅ → removed 2026-05-29 ❌ | Hexagonal fix applied ✅ | OpenAI migration: Implemented ✅ | Provider-agnostic refactor: Implemented ✅ | Claude runner implemented ✅ | Cloud Run Jobs migration: Implemented ✅
**Date:** 2026-03-03
**Implemented:** 2026-03-03 (Gemini); hexagonal fix 2026-03-03; OpenAI + provider-agnostic refactor 2026-03-07; Claude runner 2026-03-13; Cloud Run Jobs migration 2026-03-14; **Gemini backend removed 2026-05-29** (see [decisions/gemini_deep_research_adapter_removal.md](../04_solution_strategy/decisions/gemini_deep_research_adapter_removal.md))
**Owner:** AI Engineering
**Milestone:** Phase 2 — Async Specialist Agents
**Depends on:** ACP_V2_SIMPLIFIED_RFC.md (ASYNC infrastructure)

---

## 1. Executive Summary

Integrates a Deep Research specialist agent accessible via an explicit user trigger.
Deep Research performs autonomous multi-step investigation (80–160 searches, 5–60 minutes
execution) and delivers a cited long-form report as an HTML page via GCS link.

This document covers the full lifecycle: initial Gemini implementation, the hexagonal
architecture fix applied after initial release, the OpenAI migration, the Claude runner
addition, and the 2026-05-29 removal of the Gemini backend.

**Current state (post-2026-05-29):** Two backends — `ClaudeDeepResearchAdapter` (default,
runs in a Cloud Run Job) and `OpenAIDeepResearchAdapter` (webhook delivery). The Gemini
backend below is retained in this document as historical context — code has been removed.

**Phase 1 — Gemini backend (implemented):**

- `PROTOCOL_DEEP_RESEARCH_PREP` — Firestore token in Smart Agent's profile.
  Governs a two-stage interaction: clarify research intent → confirm brief → dispatch.
- `GeminiDeepResearchAdapter` — calls `google.genai` interactions client; owns Cloud Task
  enqueue logic. `DeepResearchAgent` calls the port; delivery mechanism is adapter-internal.
- `deep_research_polling` — `WorkerHandler` task type. Polls Gemini status every 120s;
  re-enqueues while pending; on completion generates HTML report, uploads to GCS,
  formats via SmartAgent, delivers via `UserNotificationService.notify_raw()`.
- `UserNotificationService.notify_raw()` — direct delivery bypassing QuickAgent.

**Phase 2 — Hexagonal architecture fix (applied, see §10):**

- `task_queue` removed from `DeepResearchAgent`. Delivery mechanism is adapter-internal.
- `DeepResearchPort.create_interaction()` extended with explicit delivery context params.
- `GeminiDeepResearchAdapter` now owns `task_queue` and polling task enqueue.
- `DeepResearchAgent` is now provider-agnostic — structurally identical to other specialists.

**Phase 3 — OpenAI migration (implemented):**

- `OpenAIDeepResearchAdapter` — implements same `DeepResearchPort`; webhook-based delivery.
- No Cloud Tasks polling needed for OpenAI path. Default provider: OpenAI.
- `GeminiDeepResearchAdapter` retained as secondary provider (polling, Cloud Tasks).

**Phase 4 — Provider-agnostic port refactor (implemented 2026-03-07):**

- `model: Optional[str]` removed from `DeepResearchPort.create_interaction()`.
  Model selection is now adapter-internal (`_resolve_model(tier)` private method).
- `get_model_for_tier()` removed from port — was leaking adapter concern onto the boundary.
- `tier: PerformanceTier` added to `create_interaction()` — the correct domain abstraction.
- `system_prompt: Optional[str]` added — assembled by agent via `PromptBuilderPort`.
  Current adapters (Gemini, OpenAI) ignore it. Future Claude adapter will use it.
- `DeepResearchAgent` follows standard agent constructor pattern:
  `(config, job_port, tier, prompt_builder, user_id)`.
- `model_override: Optional[str]` added to adapter constructors — env-var pin at startup.
- `resolve_async_context()` returns `(job_port, tier, provider_name)` — no model_name.
- Polling interval increased: 30s → 120s (Gemini rate limit: 1 RPM).
  Max attempts: 120 → 30. Total window unchanged: 60 minutes.

**Phase 5 — Claude runner (implemented 2026-03-13):**

- `ClaudeDeepResearchRunnerAgent` — internal specialist agent that executes the full research
  loop in a single Claude API session. Registered as `internal=True`, never exposed to LLMs.
- **Single-session architecture.** No multi-turn outer loop. All research happens via
  `messages.stream()`. The API manages context and tool execution server-side.
- **Native built-in tools:**
  - `web_search_20260209` — web search with dynamic filtering.
  - `web_fetch_20260209` — URL/PDF fetching with dynamic filtering.
  - `code_execution_20250825` — auto-injected by the API. NOT declared explicitly.
- **`pause_turn` continuation protocol.** When server-side `code_execution` is running, the API
  returns `stop_reason=pause_turn`. The runner appends `accumulated_content` as an assistant
  message and loops again. `container_id` must be captured from `message_delta` SSE event
  (NOT from the final Message snapshot — SDK does not propagate it there) and passed back
  in every continuation request.
- **Thinking:** `adaptive` mode with `output_config: {effort: high}` for Sonnet 4.6 / Opus 4.6.
  `temperature=1.0` required when thinking is active.
- **Prompt caching:** system prompt cached with `cache_control: ephemeral` (5 min TTL).
  All `pause_turn` continuations get a cache HIT on the system prompt.
- **Delivery chain (via Cloud Tasks):** runner returns `{text, round1_text, query}` →
  `AgentWorkerHandler._deliver_deep_research_result()` → `deliver_deep_research()`:
  (1) upload raw `.md` round files to GCS (`deep_research/{user_id}/{timestamp}-{round1|round2|report}.md`),
  send named `notify_document_link()` for each (e.g. "Round 1 — raw research", "Round 2 — verified report");
  (2) enqueue `create_html_page` Cloud Task → `HtmlPageGeneratorAgent` → styled HTML page → GCS link → user channel.
  Single-pass mode (second pass disabled): one upload labelled "Research report (raw)" + HTML page.
- **Cognitive process:** `DEEP_RESEARCH_COGNITIVE_PROCESS.groovy` — 4-phase protocol:
  Phase 0 (topic map) → Phase 1 (execute by dimension) → Phase 2 (counter-search verification)
  → Phase 3 (gap audit) → FINAL OUTPUT. Single uninterrupted session.
- **Note:** Phase 5 originally invoked via `agent_execution` Cloud Task with 1800s deadline.
  Migrated to Cloud Run Jobs in Phase 6 (see below).

**Phase 6 — Cloud Run Jobs migration (implemented 2026-03-14):**

Root cause: two research passes (first pass + second-pass critic) can exceed 60–90 minutes.
Cloud Tasks hard dispatch deadline = 1800s (30 min). Cloud Run service timeout = 3600s (1 hr).
Both ceilings were hit in production. Solution: Cloud Run Jobs (task-timeout up to 168 hours).

- **`JobRunnerPort`** (`src/ports/job_runner_port.py`) — new port. Single method:
  `run_job(job_name, env_overrides) -> str`. Fire-and-forget; does not await job completion.
- **`CloudRunJobsAdapter`** (`src/adapters/cloud_run_jobs_adapter.py`) — implements `JobRunnerPort`.
  POST to Cloud Run Jobs REST API v2 (`jobs/{name}:run`) with `containerOverrides.env`.
  Auth via `google.auth.default()` + `credentials.refresh()` in `asyncio.to_thread()` (sync ADC).
  No new dependencies — uses `aiohttp` (already in requirements) + `google.auth` (transitive).
- **`ClaudeDeepResearchAdapter`** (rewritten) — now receives `JobRunnerPort` + `job_name`.
  `create_interaction()` encodes query as `JOB_QUERY` env var, context dict as `JOB_CONTEXT_JSON`.
  Returns UUID job_id immediately. `get_status()` is a no-op stub.
- **`job_main.py`** (new, root level) — Cloud Run Job entrypoint. Reads `JOB_QUERY` +
  `JOB_CONTEXT_JSON` env vars, creates `ClaudeDeepResearchRunnerAgent` with `timeout_ms=None`
  (no Python-level timeout — job task-timeout is the ceiling), runs the agent, calls
  `deliver_deep_research()`. `sys.exit(1)` on failure.
- **`task-timeout=18000`** (5 hours) — set in `cloudbuild-dev.yaml` and `cloudbuild-prod.yaml`.
  Covers two full passes with overload retries and large research topics.
- **`max_tokens=64_000`** for thinking models (Sonnet 4.6, Opus 4.6) + `output-128k-2025-02-19`
  beta header. Previous 32K ceiling was hit in production (87K total tokens, output exhausted).
- **Second-pass critic.** After first pass `end_turn`, runner calls `_research_loop()` again
  with a critic query: includes first-pass report and asks model to find gaps and produce a new
  complete report. Controlled by `DEEP_RESEARCH_SECOND_PASS` env var (default: `true`).
  `result_text` is overwritten with second-pass output.
- **`overloaded_error` retry.** `_call_with_overload_retry()`: up to 3 retries, 30s → 60s →
  120s exponential backoff. Confirmed needed in production (observed at pause_turn #2, ~25 min in).
- **Debug logging.** `_debug_raw_turn()` called at both `end_turn` and `max_tokens` paths.
  Saves prompt + response to GCS (`DEBUG_PROMPTS_BUCKET` env var). Two files per pass:
  `{agent_type}/{date}/{ts}_prompt.txt` + `{ts}_response.txt`.
- **Cloud Run Job logs.** `resource.type=cloud_run_job`, NOT `cloud_run_revision`.
  Use `make logs-research-job-dev-tail` (added to Makefile) to tail in real time.
- **Deployment.** `make deploy-dev` passes `_DEBUG_PROMPTS_BUCKET` from `.env` to both
  the service and the job via `gcloud builds submit --substitutions`. No manual trigger
  configuration needed.

**No changes to SmartAgent code in any phase.** Preparation protocol is fully prompt-driven.

**Trigger:** user requests deep research in any phrasing (any language).
Router routes naturally to Smart (high complexity classification).

---

## 2. Problem Statement

### 2.1 Research Depth Gap

`search_web` (WebSearchAgent) = single Gemini grounding call, 15–30s latency, inline result.
Covers facts and quick lookups. Insufficient for:

- Strategic decisions: "should I use technology X in production at scale?"
- Market and competitive research: "what are the leading frameworks for Y in 2026?"
- Complex risk analysis: "what are the failure modes and risks of doing Z?"

These tasks require comprehensive multi-source investigation that a single web search call
cannot provide.

### 2.2 Clarification-Before-Dispatch Gap

A deep research task without proper scoping returns a generic, unfocused report. At $2–5
per request and 5–60 minutes execution time, a wasted call is expensive and frustrating.

The user must confirm: exactly what is being investigated, why, in what scope, and in what
output format — BEFORE the API is called.

The orchestrator (Smart Agent) is the right layer for this clarification: it has the full
conversation context and can identify gaps in the user's request without asking redundant
questions.

### 2.3 Long-form Output Delivery Gap

Slack has a message length limit. A 3000-word cited report sent as raw text would be
truncated, fragmented, or formatted poorly. The report must be delivered as a persisted
document — accessible via a single link, rendered correctly, shareable.

---

## 3. Solution Design

### 3.1 Two-Phase Protocol

```
Phase 1 — Preparation (synchronous, Smart Agent, prompt-driven):

  User:  "deep research [topic]"
  Smart: detects trigger → activates PROTOCOL_DEEP_RESEARCH_PREP
       → identifies what is missing from the request (object / goal / scope / format)
       → asks only what is missing (may be 0 questions if request is complete)
       → formulates research brief
       → presents brief: "Ось що досліджуватиму: [...]. Запускати?"
  User:  confirms ("так" / "запускай" / "yes" / any confirming phrase)
  Smart: calls intent deep_research with {query: <brief>, language: <user_language>}

Phase 2 — Execution (asynchronous):

  DeepResearchAgent:
    → client.interactions.create(input=full_query, agent='deep-research-pro-preview-12-2025',
                                  background=True)
    → receives interaction_id
    → enqueues deep_research_polling Cloud Task
    → returns ACK to Smart Agent

  Smart Agent:
    → informs user: "Запустив. Орієнтовно 5–60 хв. Повідомлю коли буде готово."

  [polling loop — Cloud Tasks, 120-second intervals]:

  WorkerHandler.deep_research_polling:
    GET interaction status → interaction.status
    "in_progress" → re-enqueue (attempt+1, delay=120s)
    "completed"   → generate HTML → GcsMediaAdapter.store() → notify_raw(url)
    "failed"      → notify_raw(error message)
    timeout (attempt >= 30, i.e. 60 min) → notify_raw(timeout message)

  Note: Gemini API quirk — `interaction.updated` field does not change when status
  transitions to "completed". Do not use it as a liveness signal.
```

### 3.2 4-Point Clarification Framework

Smart uses this framework to evaluate what is missing from the user's request.
Smart asks only what is missing. If the request is already complete, Smart skips
directly to brief formulation without asking any questions.

```
1. Research object
   What exactly? Specific, unambiguous.
   "AI" → insufficient. "Gemini 2.5 Pro adoption in production RAG systems" → sufficient.

2. Goal and context
   Why? What decision, action, or output does this research enable?
   Different goals produce fundamentally different reports.
   "понять рынок" vs "выбрать между A и B" vs "написать статью" → different structure.

3. Scope and constraints
   Focus area: technical / business / historical / comparative / legal.
   Time period, geography, depth, what to explicitly exclude.

4. Expected output format
   Brief executive summary / detailed report / option comparison / source list.
   Level of technical detail, required sections, length preference.
```

Questions 1+2 are often inferrable from context. Questions 3+4 are most commonly missing.
Smart groups related gaps into a single natural question rather than asking one by one.

### 3.3 Language Passing

Smart includes `language` in the `deep_research` intent payload.

```python
payload = {
    "query":    "<complete research brief>",
    "language": "Ukrainian",  # Smart detects from conversation
}
```

DeepResearchAgent appends a language instruction to the research brief before sending to
Gemini:

```
{query}

Please write the entire response in {language}.
```

### 3.4 Plan Approval — Not Applicable

The current Gemini Deep Research API preview does not support human-approved planning or
structured plan approval calls. The API executes autonomously after `interactions.create()`.
No `_approve_plan()` step exists or is needed. The user's brief confirmation in Phase 1
is the only approval gate.

### 3.5 Report Delivery via GCS

Deep Research output is a long-form markdown report (2000–5000+ words with citations).
Delivered as an HTML page uploaded to GCS, with the public URL sent to the user.

```
interaction.outputs[-1].text    ← markdown report from Gemini
  → wrap in HTML template (MVP) or LLM-generated HTML (v2)
  → GcsMediaAdapter.store(html_bytes, key, "text/html; charset=utf-8")
  → public URL
  → notify_raw(user_id, account_id, url)
```

**MVP HTML template** (no LLM, no dependencies):
```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Deep Research Report</title>
  <style>
    body { max-width: 860px; margin: 40px auto; padding: 0 20px;
           font-family: Georgia, serif; line-height: 1.7; color: #222; }
    h1, h2, h3 { font-family: system-ui, sans-serif; }
    a { color: #0066cc; }
    blockquote { border-left: 3px solid #ccc; margin: 0; padding-left: 1em; color: #555; }
  </style>
</head>
<body>{{html_content}}</body>
</html>
```

Markdown → HTML conversion via Python `markdown` library (stdlib-level dependency, already
commonly available). GCS key: `deep_research/{user_id}/{timestamp_iso}.html`.

**v2 (future):** LLM generates a visually richer HTML page with structured sections,
highlighted citations, and a table of contents.

### 3.6 AgentDescriptor internal flag

`deep_research` intent is registered with `internal=False` — it appears in Smart Agent's
tool list. The capability description is explicitly constrained ("ONLY when user explicitly
requests"). The `PROTOCOL_DEEP_RESEARCH_PREP` token is the primary gate.

---

## 4. Component Design

### 4.1 PROTOCOL_DEEP_RESEARCH_PREP (Firestore token)

**Location:** Firestore, category `protocols`, class `PROTOCOL_DEEP_RESEARCH_PREP`.
**Upload:** `firestore_utils/uploads/PROTOCOL_DEEP_RESEARCH_PREP.json`.
**Profile:** added to `universal_agent_v1_SYSTEM_smart` alongside existing protocol tokens.

Groovy DSL specification:

```groovy
PROTOCOL_DEEP_RESEARCH_PREP {

    trigger {
        // Activate this protocol ONLY when the user's message contains
        // "deep research" or "дип ресерч" (case-insensitive, anywhere in the message).
        // Do NOT activate for general research questions without the explicit trigger phrase.
    }

    clarification_framework {
        // Before dispatching the deep_research intent, ensure you have:
        //   1. Research object — specific, unambiguous (not "AI" but "Gemini 2.5 Pro for RAG")
        //   2. Goal & context — what decision, action, or output this research enables
        //   3. Scope & constraints — focus area, time period, depth, what to exclude
        //   4. Expected output format — executive summary / detailed report / comparison / sources

        // RULE: Only ask what is MISSING from the user's request.
        // If the request specifies all four points → proceed directly to brief formulation.
        // If multiple gaps exist → group into one natural question, not a numbered list.
        // Maximum 2 clarification rounds before proceeding with best-effort assumptions.
    }

    brief_confirmation {
        // When all four points are known, formulate the research brief and present it:
        //   "Ось що досліджуватиму: [brief]. Запускати?"
        // Use the user's language for this confirmation message.
        // Wait for an explicit confirming response before dispatching.
        // If the user modifies the brief → update and re-confirm once.
        // If the user declines → offer to adjust the brief.
    }

    dispatch {
        // After user confirms: call delegate_to_specialist with:
        //   intent: "deep_research"
        //   query: <complete research brief, including all four points>
        //   language: <user's current language>
        // Inform the user: research started, estimated 5–60 minutes, report link on completion.
    }

}
```

### 4.2 DeepResearchAgentConfig

```python
# src/infrastructure/agent_config.py

@dataclass
class DeepResearchAgentConfig:
    """
    Behavioral parameters for DeepResearchAgent.

    timeout_ms covers the interactions.create() kick-off call only (returns quickly).
    The 5–60 minute research execution runs via Cloud Tasks polling, not within this timeout.
    """
    context_window: int = 32_000
    timeout_ms: int     = 30_000   # kick-off only
    max_retries: int    = 2

DEEP_RESEARCH = DeepResearchAgentConfig()
```

### 4.3 DeepResearchAgent

Standard agent constructor pattern (Phase 4 refactor). No LLMPort — uses DeepResearchPort.

```python
# src/agents/deep_research_agent.py

class DeepResearchAgent(BaseAgent):
    """
    Submits a background research job via DeepResearchPort and returns ACK.

    Execution mode: SYNC — returns after kicking off the async operation (~seconds).
    Does NOT use LLMPort / AgentExecutionContext: the Deep Research API is outside
    the standard LLM provider interface and is accessed directly via DeepResearchPort.
    """

    TIMEOUT_MS  = DEEP_RESEARCH.timeout_ms
    MAX_RETRIES = DEEP_RESEARCH.max_retries

    def __init__(
        self,
        config: AgentConfig,
        job_port: DeepResearchPort,
        tier: PerformanceTier = PerformanceTier.BALANCED,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self._job_port      = job_port
        self._tier          = tier
        self._prompt_builder = prompt_builder
        self._user_id       = user_id

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query      = message.payload.get("query", "")
        brief      = message.payload.get("brief", query)
        language   = message.payload.get("language", "English")
        user_id    = message.context.get("user_id", "")
        account_id = message.context.get("account_id", "")

        system_prompt = ""
        if self._prompt_builder:
            try:
                system_prompt = await self._prompt_builder.build_for_agent(
                    "deep_research", self._user_id
                )
            except Exception as e:
                logger.warning("[DeepResearchAgent] PromptBuilder failed: %s", e)

        full_query = f"{query}\n\nPlease write the entire response in {language}."

        job_id = await self._job_port.create_interaction(
            query=full_query,
            user_id=user_id,
            account_id=account_id,
            original_query=brief[:512],
            tier=self._tier,
            system_prompt=system_prompt or None,
        )

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={"status": "started", "interaction_id": job_id},
        )
```

**Key design decisions:**
- `tier` replaces `model_name` — domain abstraction, not provider string.
- `system_prompt` assembled here, passed to port. Adapters decide whether to use it.
- No `task_queue` in agent — delivery is adapter-internal.
- Factory passes `prompt_builder` and `user_id` identically to all other specialists.

### 4.4 TaskQueue Port — new method

```python
# src/ports/task_queue.py

async def enqueue_deep_research_polling(
    self,
    interaction_id: str,
    user_id: str,
    account_id: str,
    attempt: int = 0,
    delay_seconds: int = 30,
) -> str:
    """
    Enqueue a deep_research_polling Cloud Task.

    Worker receives payload with task_type="deep_research_polling".
    attempt tracks retry count for timeout guard (max 30 attempts × 120s = 60 min).
    delay_seconds: schedule task this many seconds in the future (Cloud Tasks schedule_time).
    First enqueue (attempt=0) uses delay_seconds=120 — Gemini rate limit: 1 RPM.
    """
    ...
```

**Implementation note for Cloud Tasks adapter:** Use
`schedule_time = now + timedelta(seconds=delay_seconds)` in the Cloud Tasks request.
This is the first method requiring scheduled delivery. Verify adapter implementation.

### 4.5 WorkerHandler — deep_research_polling task type

```python
# src/handlers/worker_handler.py

# In handle():
elif task_type == "deep_research_polling":
    return await self._handle_deep_research_polling(payload)

async def _handle_deep_research_polling(self, payload: dict) -> Tuple[dict, int]:
    """
    Poll Gemini interaction status. Re-enqueue if in_progress.
    On completion: generate HTML report → upload to GCS → send URL to user.

    Timeout guard: 30 attempts × 120s = 60 min = Gemini Deep Research max window.
    Polling interval 120s chosen for Gemini rate limit (1 RPM on interactions.get).
    """
    interaction_id = payload.get("interaction_id")
    user_id        = payload.get("user_id")
    account_id     = payload.get("account_id")
    attempt        = payload.get("attempt", 0)

    MAX_ATTEMPTS = 30

    if attempt >= MAX_ATTEMPTS:
        logger.warning(f"[DeepResearch] Polling timeout: interaction={interaction_id[:16]}")
        await self._notification.notify_raw(
            user_id=user_id,
            account_id=account_id,
            text="Deep research timed out after 60 minutes without a result.",
        )
        return {"status": "timeout"}, 200

    interaction = await asyncio.get_event_loop().run_in_executor(
        None, lambda: self._gemini_client.interactions.get(interaction_id)
    )

    if interaction.status == "in_progress":
        await self._task_queue.enqueue_deep_research_polling(
            interaction_id=interaction_id,
            user_id=user_id,
            account_id=account_id,
            attempt=attempt + 1,
            delay_seconds=120,
        )
        logger.info(f"[DeepResearch] In progress, attempt={attempt + 1}")
        return {"status": "polling", "attempt": attempt + 1}, 200

    if interaction.status == "completed":
        report_md = interaction.outputs[-1].text
        url = await self._build_and_upload_report(report_md, user_id)
        await self._notification.notify_raw(
            user_id=user_id,
            account_id=account_id,
            text=url,
        )
        logger.info(f"[DeepResearch] Delivered to user={user_id[:8]}, url={url}")
        return {"status": "delivered"}, 200

    # status == "failed"
    error = getattr(interaction, "error", "unknown error")
    logger.error(f"[DeepResearch] Failed: interaction={interaction_id[:16]}, error={error}")
    await self._notification.notify_raw(
        user_id=user_id,
        account_id=account_id,
        text=f"Deep research failed: {error}",
    )
    return {"status": "failed"}, 200

async def _build_and_upload_report(self, markdown_text: str, user_id: str) -> str:
    """
    Convert markdown report to HTML, upload to GCS, return public URL.

    MVP: minimal HTML template wrapping markdown-converted content.
    v2: LLM-generated HTML with richer structure and visual formatting.
    """
    import markdown as md_lib
    from datetime import datetime

    html_body  = md_lib.markdown(markdown_text, extensions=["tables", "fenced_code"])
    html_page  = _REPORT_HTML_TEMPLATE.replace("{{html_content}}", html_body)
    html_bytes = html_page.encode("utf-8")

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    key = f"deep_research/{user_id}/{timestamp}.html"

    url = await self._media_storage.store(
        data=html_bytes,
        key=key,
        content_type="text/html; charset=utf-8",
    )
    return url


_REPORT_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Deep Research Report</title>
  <style>
    body { max-width: 860px; margin: 40px auto; padding: 0 20px;
           font-family: Georgia, serif; line-height: 1.7; color: #222; }
    h1, h2, h3 { font-family: system-ui, sans-serif; }
    a { color: #0066cc; }
    blockquote { border-left: 3px solid #ccc; margin: 0;
                 padding-left: 1em; color: #555; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background: #f5f5f5; }
    code { background: #f4f4f4; padding: 2px 4px; border-radius: 3px; }
    pre code { display: block; padding: 1em; overflow-x: auto; }
  </style>
</head>
<body>{{html_content}}</body>
</html>"""
```

**WorkerHandler constructor**: add `gemini_client` and `media_storage: MediaStoragePort`
parameters. Both are available in `ServiceContainer` and can be passed from `main.py`.

### 4.6 UserNotificationService.notify_raw()

```python
# src/services/user_notification_service.py

async def notify_raw(
    self,
    user_id: str,
    account_id: str,
    text: str,
) -> None:
    """
    Deliver text directly to user's last active channel. No agent reformatting.

    Used for Deep Research report delivery: sends the GCS URL as a plain message.
    Contrast with notify() which routes through QuickAgent for style formatting.
    """
    try:
        channel_info = await self._state_repo.get(user_id)
    except Exception as exc:
        logger.warning(f"[Notification] Failed to load channel for {user_id[:8]}: {exc}")
        return

    if not channel_info:
        logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping")
        return

    response_channel = self._channel_factory.create(
        platform=channel_info.platform,
        channel_id=channel_info.channel_id,
    )
    if not response_channel:
        logger.warning(
            f"[Notification] Cannot create channel: platform={channel_info.platform}"
        )
        return

    try:
        await response_channel.send_message(text)
        logger.info(
            f"📬 [Notification] Raw delivery to {channel_info.platform} "
            f"channel={channel_info.channel_id} user={user_id[:8]}"
        )
    except Exception as exc:
        logger.error(
            f"[Notification] Raw delivery failed for {user_id[:8]}: {exc}",
            exc_info=True,
        )
```

### 4.7 AgentDescriptor Registration

```python
# main.py

registry.register(AgentDescriptor(
    agent_id="deep_research_agent",
    agent_type="deep_research",
    capabilities={"deep_research": ExecutionMode.SYNC},
    capability_descriptions={
        "deep_research": (
            "Autonomous deep research via Gemini Deep Research API. "
            "Executes 80–160 searches over 5–60 minutes and returns a cited long-form report "
            "as a public HTML link. "
            "Use ONLY when the user has explicitly requested 'deep research' and confirmed "
            "the research brief. NOT for quick facts, NOT for regular web search. "
            "Result is delivered asynchronously as a link — inform the user it will arrive separately. "
            "payload: {\"query\": \"<complete research brief>\", \"language\": \"<language>\"}"
        ),
    },
    internal=False,
))
```

### 4.8 UserAgentFactory Instantiation

```python
# src/composition/user_agent_factory.py

def _create_deep_research_agent(self, user_id: str) -> DeepResearchAgent:
    return DeepResearchAgent(
        config=DEEP_RESEARCH,
        gemini_client=self._gemini_client,   # google.genai.Client from ServiceContainer
        task_queue=self._task_queue,
    )
```

**Note:** DeepResearchAgent does not use the provider abstraction layer (`AgentContextBuilder`,
`ProviderRegistry`) — it calls the Gemini SDK directly. The Deep Research API is not
accessible through the standard `LLMPort` interface. No `execution_context` needed.

---

## 5. End-to-End Flow

```
User: "deep research: порівняй Supabase і PlanetScale для multi-tenant SaaS в 2026"

1. Router: high complexity → Smart Agent

2. Smart Agent (PROTOCOL_DEEP_RESEARCH_PREP):
   - Request contains all 4 points:
     object: Supabase vs PlanetScale
     goal: choose for multi-tenant SaaS
     scope: 2026, comparative
     format: comparison (implied)
   - No clarifying questions needed
   - Brief: "Порівняю Supabase і PlanetScale для multi-tenant SaaS продукту в 2026 —
     архітектура, pricing, performance, обмеження, реальні кейси"
   - Presents: "Ось що досліджуватиму: [...]. Запускати?"

3. User: "так"

4. Smart Agent: delegate_to_specialist(
     intent="deep_research",
     query="<brief>",
     language="Ukrainian"
   )

5. AgentCoordinator → DeepResearchAgent.process():
   - client.interactions.create(input=full_query,
                                 agent='deep-research-pro-preview-12-2025',
                                 background=True)
   - interaction_id = "interactions/xyz789"
   - task_queue.enqueue_deep_research_polling(interaction_id, user_id, account_id)
   - Returns {status: "started", interaction_id: "..."}

6. Smart Agent → user:
   "Запустив. Gemini проводить дослідження — орієнтовно 5–60 хв.
    Надішлю посилання на звіт як буде готово."

[5–60 minutes — Cloud Tasks polling loop]

7. WorkerHandler (deep_research_polling, attempt=0..N):
   interactions.get(interaction_id) → status="in_progress"
   → re-enqueue(attempt+1, delay=30s)

8. WorkerHandler (attempt=N):
   interactions.get(interaction_id) → status="completed"
   report_md = interaction.outputs[-1].text   # full cited report in Ukrainian
   html = markdown(report_md) wrapped in HTML template
   url = GcsMediaAdapter.store(html, "deep_research/{user_id}/{ts}.html", "text/html")
   notification_service.notify_raw(user_id, account_id, url)

9. User receives GCS link in Slack/Telegram.
   Opens HTML page: full structured report with citations, headers, tables.
```

---

## 6. Implementation Plan

### Step 1: UserNotificationService.notify_raw()

- [ ] Add `notify_raw()` to `UserNotificationService`
- [ ] Unit test: mock `state_repo.get()` and `response_channel.send_message()`,
      verify direct delivery without coordinator involvement

### Step 2: TaskQueue port + adapter

- [ ] Add `enqueue_deep_research_polling()` to `src/ports/task_queue.py`
- [ ] Implement in Cloud Tasks adapter with `delay_seconds` → `schedule_time` support
- [ ] Unit test: verify `schedule_time = now + timedelta(seconds=delay_seconds)`

### Step 3: DeepResearchAgentConfig

- [ ] Add `DeepResearchAgentConfig` + `DEEP_RESEARCH` constant to `agent_config.py`

### Step 4: DeepResearchAgent

- [ ] Create `src/agents/deep_research_agent.py`
- [ ] Verify `google.genai` SDK availability and `interactions.create()` in the project
- [ ] Add `DEEP_RESEARCH_MODEL` constant with current model name
- [ ] Add to `src/agents/__init__.py`
- [ ] Unit test: mock `gemini_client.interactions.create()`, verify enqueue called

### Step 5: WorkerHandler — deep_research_polling

- [ ] Add `deep_research_polling` branch to `WorkerHandler.handle()`
- [ ] Implement `_handle_deep_research_polling()` with timeout guard
- [ ] Implement `_build_and_upload_report()`: markdown → HTML → GCS
- [ ] Add `gemini_client` and `media_storage: MediaStoragePort` to `WorkerHandler.__init__()`
- [ ] Verify `markdown` library available (`pip install markdown`) — add to requirements if not
- [ ] Unit test: mock interactions.get() for in_progress / completed / failed / timeout

### Step 6: AgentDescriptor + UserAgentFactory

- [ ] Register `deep_research_agent` in `main.py`
- [ ] Add `_create_deep_research_agent()` to `UserAgentFactory`
- [ ] Pass `gemini_client` from `ServiceContainer` to `UserAgentFactory`

### Step 7: Firestore prompt token

Manual step (owner task):
- [ ] Author `PROTOCOL_DEEP_RESEARCH_PREP` token (Groovy DSL, §4.1 as spec)
- [ ] Upload via `firestore_utils/upload.py`
- [ ] Add to `universal_agent_v1_SYSTEM_smart` profile

---

## 7. Open Questions & Decisions

### Q1: Gemini Deep Research API — SDK shape ✅ RESOLVED

**Resolved 2026-03-03** (from official API reference).

**Actual API shape:**

```python
from google import genai

client = genai.Client()

# Kick-off
interaction = client.interactions.create(
    input="research brief",
    agent="deep-research-pro-preview-12-2025",
    background=True,
)
interaction_id = interaction.id

# Poll
interaction = client.interactions.get(interaction_id)
# interaction.status: "in_progress" | "completed" | "failed"
# interaction.outputs[-1].text  ← full report (when completed)
# interaction.error              ← error string (when failed)
```

**No plan approval mechanism** — Deep Research API preview does not support it.
Agent executes autonomously after `interactions.create()`.

**`store=True` does not exist** — removed from RFC.
**Model name:** `deep-research-pro-preview-12-2025`.
**`previous_interaction_id`** — can continue from a prior completed research. Not needed for MVP.

---

### Q2: Polling — Cloud Tasks vs Cloud Scheduler ✅ DECIDED

**Decision:** Self-re-enqueuing Cloud Tasks.
Each operation carries its own state in the Cloud Tasks payload.
No Firestore collection needed. Correct for solo exocortex scale.

---

### Q3: Plan summary surfacing to user ✅ NOT APPLICABLE

Plan approval does not exist in the current API preview. Q3 is moot.
Smart informs the user of estimated time only (§3.1).

---

### Q4: Report delivery format ✅ DECIDED

**Decision:** GCS HTML upload + URL link.

Report (markdown) → HTML via `markdown` library + minimal CSS template →
`GcsMediaAdapter.store()` → public URL → `notify_raw(url)`.

No Slack message length issues. Report is persistent, shareable, correctly rendered.

**v2 path:** LLM-generated HTML with richer visual structure (table of contents,
citation highlights, executive summary box).

---

## 8. Success Criteria

### Functional

- User writes "deep research [topic]" → Smart enters preparation protocol ✅
- Smart asks only missing information (0–2 rounds) ✅
- Smart presents research brief for confirmation ✅
- After confirmation: research starts within 5s ✅
- User receives "research started" acknowledgment ✅
- User receives GCS report link within 60 minutes ✅
- Report link opens a readable HTML page with citations intact ✅
- Failure results in error notification (not silent) ✅

### Quality

- Research report is in user's language ✅
- Citations and source links preserved in HTML output ✅
- HTML page renders correctly on mobile and desktop ✅

### Infrastructure

- Polling stops after 60 minutes regardless of operation status ✅
- Polling timeout sends error notification (not silent failure) ✅
- WorkerHandler handles `deep_research_polling` independently of other task types ✅
- `notify_raw()` sends URL directly — QuickAgent not invoked ✅

---

## 10. Hexagonal Architecture Fix (applied 2026-03-03)

### 10.1 Problem

The initial implementation had `task_queue: Optional[TaskQueue]` in `DeepResearchAgent`'s
constructor — an infrastructure leak. The agent knew about Cloud Tasks, which violated the
hexagonal principle that agents depend only on ports, not on infrastructure.

Additionally, `DeepResearchPort.create_interaction()` took only `query: str` — the adapter
had no way to enqueue the polling task without the agent passing delivery context directly.

### 10.2 Fix

**`DeepResearchPort.create_interaction()` — new signature:**

```python
async def create_interaction(
    self,
    query: str,
    user_id: str,
    account_id: str,
    original_query: str,
) -> str:
    """
    Start a Deep Research operation and arrange for result delivery.

    Delivery mechanism is adapter-specific:
      GeminiDeepResearchAdapter → enqueues deep_research_polling Cloud Task.
      OpenAIDeepResearchAdapter → embeds user_id/account_id/original_query as OpenAI
                                  metadata; OpenAI echoes them back in the webhook payload.
    """
```

**`GeminiDeepResearchAdapter`** — now owns `task_queue: Optional[TaskQueue]` in constructor.
`create_interaction()` calls Gemini + immediately enqueues the polling task.
Guards early (before calling Gemini) if `task_queue is None` — no wasted API calls in local dev.

**`DeepResearchAgent`** — `task_queue` removed entirely. Constructor is now:
```python
def __init__(self, config: AgentConfig, deep_research_port: DeepResearchPort) -> None:
```
Calls port with explicit delivery context:
```python
interaction_id = await self._dr_port.create_interaction(
    query=full_query,
    user_id=user_id,
    account_id=account_id,
    original_query=query,
)
```

**`UserAgentFactory`** — `task_queue` parameter removed entirely.
**`main.py`** — `task_queue=agent_task_queue` moved from `UserAgentFactory(...)` to
`GeminiDeepResearchAdapter(api_key=..., task_queue=agent_task_queue)`.

### 10.3 Result

`DeepResearchAgent` is structurally identical to every other specialist agent.
Switching Deep Research backends = swap adapter in `main.py`. No agent code changes needed.

---

## 11. OpenAI Migration (proposed)

### 11.1 Why Migrate

`deep-research-pro-preview-12-2025` is a preview API with observed March 2026 instability:
stuck `in_progress` for 60+ minutes, HTTP 500 on stale connections, no SLA.
The polling loop itself accumulates accidental complexity: 120 Cloud Tasks × 30s, stale
HTTP connection workarounds, `consecutive_errors` heuristics.

OpenAI Deep Research (Responses API) delivers results via webhooks — no polling needed.

### 11.2 OpenAIDeepResearchAdapter Design

```python
# src/adapters/openai_deep_research_adapter.py

class OpenAIDeepResearchAdapter(DeepResearchPort):
    """
    DeepResearchPort implementation backed by OpenAI Responses API (background=True).

    Delivery model: WEBHOOK — OpenAI calls /webhooks/openai/deep-research on completion.
    No task_queue dependency — adapter does not enqueue Cloud Tasks.

    user_id/account_id/original_query embedded as OpenAI metadata; echoed in webhook payload.
    """

    def __init__(self, api_key: str, webhook_url: str) -> None:
        self._client      = OpenAI(api_key=api_key)
        self._webhook_url = webhook_url
        self._model       = os.environ.get("DEEP_RESEARCH_MODEL", "o3-deep-research-2025-06-26")

    async def create_interaction(
        self,
        query: str,
        user_id: str,
        account_id: str,
        original_query: str,
    ) -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.responses.create(
                model=self._model,
                input=[{"role": "user", "content": query}],
                background=True,
                webhook_url=self._webhook_url,
                metadata={
                    "user_id": user_id,
                    "account_id": account_id,
                    "query": original_query,   # bare query, no language suffix
                },
            ),
        )
        logger.info(f"[DeepResearch] OpenAI job created: {response.id[:16]}, model={self._model}")
        return response.id

    async def get_interaction_status(self, interaction_id: str) -> tuple[str, str]:
        # Emergency fallback — primary delivery is webhook. Retain for interface compliance.
        ...
```

Note: `OpenAIDeepResearchAdapter` uses OpenAI's **Responses API** — a background research
endpoint distinct from Chat Completions. A future general `OpenAIAdapter(LLMPort)` for
Quick/Smart agents would use Chat Completions and go through `AgentProviderStrategy` as a
separate provider — these are orthogonal concerns.

### 11.3 Webhook Endpoint

```python
# src/web/deep_research_webhooks.py

@blueprint.post("/webhooks/openai/deep-research")
async def handle_openai_deep_research():
    payload = await request.get_data()
    # Verify HMAC-SHA256 signature (webhook-signature header)
    # Extract result_text, user_id, account_id, query from metadata
    # → worker_handler._build_and_upload_report() → GCS URL
    # → worker_handler._format_via_smart_agent() → formatted message
    # → notification_service.notify_raw()
    return jsonify({"ok": True}), 200
```

Webhook events: `response.completed` → deliver report; `response.failed` → notify user;
`response.cancelled` → notify user.

HMAC secret: `OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET` (`.env` / GCP Secret Manager only).

### 11.4 Model Tiers

| Model | Quality | Latency | Cost |
|---|---|---|---|
| `o3-deep-research-2025-06-26` | High | 10–30 min | Higher |
| `o4-mini-deep-research-2025-06-26` | Good | 5–15 min | Lower |

Default: `o3-deep-research-2025-06-26`. Override: `DEEP_RESEARCH_MODEL` env var.

### 11.5 What Is Retired on Migration

- `WorkerHandler._handle_deep_research_polling()` — polling handler removed.
- `enqueue_deep_research_polling()` from `TaskQueue` port and `GcpTaskQueue` — removed.
- `GeminiDeepResearchAdapter` — moved to `archive/`.
- `consecutive_errors` counter, stale connection workarounds — no longer needed.

Shared helpers `_build_and_upload_report()` and `_format_via_smart_agent()` in
`WorkerHandler` are reused as-is by the webhook handler.

### 11.6 Migration Checklist

- [ ] Implement `OpenAIDeepResearchAdapter` + unit test
- [ ] Implement `/webhooks/openai/deep-research` Quart blueprint
- [ ] Register blueprint in main web app; pass `WORKER_HANDLER` + webhook secret
- [ ] Swap adapter in `main.py` (remove Gemini, add OpenAI)
- [ ] Remove `_handle_deep_research_polling()` from `WorkerHandler`
- [ ] Remove `enqueue_deep_research_polling()` from `TaskQueue` port + `GcpTaskQueue`
- [ ] Move `GeminiDeepResearchAdapter` to `archive/`
- [ ] Add `OPENAI_API_KEY`, `OPENAI_DEEP_RESEARCH_WEBHOOK_URL`, `OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET`
      to GCP Secret Manager (owner task)
- [ ] UAT: trigger deep research, verify webhook received + report delivered

---

## 9. References

**Depends on:**
- [ACP_V2_SIMPLIFIED_RFC.md](./ACP_V2_SIMPLIFIED_RFC.md) — ASYNC infrastructure
- [EXTENDED_WEB_SEARCH_RFC.md](./EXTENDED_WEB_SEARCH_RFC.md) — prior ASYNC agent design

**Uses:**
- `GcsMediaAdapter` / `MediaStoragePort` — already implemented for HTML widgets and map images
- `UserNotificationService` — extended with `notify_raw()`

**Building Blocks:**
- [Multi-Agent System](../05_building_blocks/multi_agent_system/README.md)
- [Agent Registry](../05_building_blocks/agent_registry/README.md)

**External:**
- Gemini Deep Research API: `https://ai.google.dev/gemini-api/docs/deep-research`
- OpenAI Deep Research: `https://platform.openai.com/docs/guides/deep-research`
- OpenAI Responses API: `https://platform.openai.com/docs/guides/background`
- OpenAI Webhooks: `https://platform.openai.com/docs/guides/webhooks`

---

## Changelog

### 2026-03-03 (revision 3 — OpenAI migration design)

- Added §11 (OpenAI migration): `OpenAIDeepResearchAdapter`, webhook endpoint, model tiers,
  migration checklist, what gets retired.
- Note on OpenAI Responses API vs Chat Completions — orthogonal to future `OpenAIAdapter(LLMPort)`.

### 2026-03-03 (revision 2 — hexagonal architecture fix)

- Added §10 (hexagonal fix): `task_queue` removed from `DeepResearchAgent`; moved to
  `GeminiDeepResearchAdapter`. Port `create_interaction()` extended with delivery context params.
  `DeepResearchAgent` now provider-agnostic — identical structure to other specialist agents.
- Merged from separate `OPENAI_DEEP_RESEARCH_RFC.md` (deleted — one RFC per feature).
- RFC title updated to "Deep Research Integration" (not Gemini-specific).

### 2026-03-07 (revision 4 — delivery deduplication)

- **Extracted `src/handlers/deep_research_delivery.py`** — shared module with `upload_html_report()`,
  `deliver_deep_research()`, and `NotificationPort` Protocol. Eliminates 3 copies of identical
  delivery logic across `worker_handler`, `agent_worker_handler`, and `deep_research_webhooks`.
- **Fixed circular import:** `agent_worker_handler` no longer uses deferred import of
  `_upload_html_report` from `worker_handler` — both import from `deep_research_delivery`.
- **Fixed `ClaudeDeepResearchRunnerAgent` RuntimeError:** two distinct paths now produce
  distinct error messages — "Exceeded N turns" vs "Unexpected stop_reason=... with no text".

### 2026-03-07 (revision 3 — delivery unification)

- **Eliminated `DeepResearchDeliveryService` and `SmartAgentFormatter`** — both duplicated
  existing `UserNotificationService.notify()` pattern. All 3 delivery paths (Gemini polling,
  OpenAI webhook, Claude runner) now use `UserNotificationService` directly.
- **Two parallel notifications on completion:** (1) `notify(agent_id_override="smart_response_agent_...")`
  for SmartAgent-formatted summary, (2) `notify_raw(url)` for direct HTML report link.
- **`session_id` propagated** through all paths: polling payload, webhook metadata, agent context.
- `UserNotificationService.notify()` gained `session_id` param (defaults to `uuid4()` for
  standalone notifications, preserves original for deep research delivery).

### 2026-03-03 (revision 1 — production fixes)

- Added stale HTTP connection fix: two-layer `genai.Client` lifecycle management
  (Layer 1: proactive 20-min recreation; Layer 2: reactive recreation + retry on 500).
- Added `consecutive_errors` counter (5 consecutive failures → declare dead, notify user).
- Added SmartAgent formatting of completed report (before: raw URL; after: formatted).
- Added `query` field carried through Cloud Task payload chain (for SmartAgent context).
- Fixed Firestore indexes deploying to `(default)` DB instead of `us-production`.

### 2026-03-03 (initial)

- Initial RFC drafted
- Two-phase protocol defined: preparation (Smart, prompt-driven) + execution (async polling)
- 4-point clarification framework specified for PROTOCOL_DEEP_RESEARCH_PREP
- Polling strategy: self-re-enqueuing Cloud Tasks — correct for solo exocortex scale
- **Q1 resolved:** actual Gemini SDK shape confirmed from API reference —
  `client.interactions.create()` / `client.interactions.get()` / `interaction.status`
- **Q3 resolved (not applicable):** plan approval does not exist in current API preview
- **Q4 resolved:** GCS HTML upload + URL link — reuses existing `GcsMediaAdapter` +
  `MediaStoragePort`; no Slack message length issues; report is persistent and shareable
- `notify_raw()` sends GCS URL directly to user, no QuickAgent involvement
- `DeepResearchAgent` uses `gemini_client` directly (not via LLMPort abstraction —
  Deep Research API is outside the standard provider interface)
