# Grok revival: the "xAI blocks Google Cloud" belief was a trailing newline

**Date:** 2026-08-14
**Status:** Implemented
**Scope:** `src/adapters/grok_adapter.py`, `src/domain/billing.py`, Secret Manager `XAI_API_KEY`

## Context

Grok had been carried as a registered-but-unused provider for months. The standing explanation was
that xAI rejected requests originating from Google Cloud, so the integration was never given
default traffic and was left un-maintained. No ADR, incident record, or log entry ever documented
that rejection — it survived as folklore. Two artifacts in the adapter were its only trace: a
blocking `socket.gethostbyname("api.x.ai")` pre-check in the constructor ("Purpose: Identify if
api.x.ai is resolvable in Cloud Run") and a custom `User-Agent: alek-bot/1.0` header.

The trigger to re-examine it was xAI's pricing: grok-4.6 shipped 2026-08-12 at $2/$6 per 1M tokens,
undercutting Sonnet 5's $2/$10 on output.

## Investigation

A throwaway Cloud Run Job on the deployed image (`alek-bot-dev:latest`), in the same region, with
the same service account and egress path as the live service:

| Key as mounted | `api_key.strip()` |
|---|---|
| 0/15 requests, `LLMNetworkError` | 15/15 HTTP 200, tool calls and JSON mode included |

`XAI_API_KEY` in Secret Manager was **85 bytes ending in `0a`**; the `.env` copy was 84. Cloud Run
mounts the secret verbatim while `.env` loading strips the newline — so Grok worked locally and
failed only in the cloud.

The mechanism: `httpx` refuses to send a header value containing `\n`
(`LocalProtocolError: Illegal header value`), the OpenAI SDK wraps that as `APIConnectionError`,
and the adapter mapped it to `LLMNetworkError("Connection error.")`. **The request never left the
container.** A one-byte data defect presented as a vendor-side network block.

90 days of Cloud Run logs contain zero `[GrokAdapter] Request:` lines — the adapter had never
successfully been called in production.

## Decisions

1. **`api_key.strip()` in the adapter constructor**, on top of fixing the secret. Defence in depth:
   the failure mode is silent and mimics an infrastructure fault, so the choke point should not be
   one newline away from a multi-month misdiagnosis.
2. **Removed the DNS pre-check and custom User-Agent.** Both chase a problem that never existed,
   and the DNS call was synchronous I/O in a constructor.
3. **Retargeted `MODEL_TIERS` to live model IDs** (ECO/BALANCED → `grok-4.3`,
   PERFORMANCE/ULTRA → `grok-4.6`). The previous `grok-4-1-fast-*` IDs are retired: absent from
   `GET /v1/models`, they still answer HTTP 200 while xAI silently serves `grok-4.3`. That does not
   fail loudly, it **mis-bills** — we priced $0.20/$0.50 for a model charging $1.25/$2.50. Same
   class of defect as TD-7 (price attribution).
4. **Migrated the transport to `/v1/responses`.** `search_parameters` on chat/completions now
   returns HTTP 410 ("Live search is deprecated. Please switch to the Agent Tools API") and a
   `{"type": "web_search"}` tool there returns HTTP 422. Server-side search exists only on the
   Responses API — the same boundary `OpenAIAdapter` already speaks, so the two adapters now share
   one dialect instead of two.
5. **Cached tokens are now measured.** xAI caches automatically and reports
   `input_tokens_details.cached_tokens`; the adapter subtracts them from `prompt_tokens` (repo
   convention: uncached input only) and bills the cached leg via `cache_read` multipliers.
   `context_caching` stays `False` — it gates `PromptCacheStrategy`, which places explicit
   Anthropic-style breakpoints, and xAI exposes no such control. False means "no controllable
   caching", not "no caching".
6. **`reasoning` output items are captured as `thought_text`**, never merged into the answer —
   mirroring the Gemini thought-parts handling. grok-4.6 reasons by default and those tokens are
   billed as output regardless.

## Feature parity with OpenAIAdapter (same day, second pass)

The transport migration alone left Grok behind on features the other adapters had accumulated.
Brought in sync, each verified against the live API rather than inferred:

7. **`PROMPT_CACHE_BOUNDARY` is stripped.** Grok was the only adapter that did not (openai 5
   references, claude 3, gemini 3, grok 0), so the literal `<!-- CACHE_BOUNDARY -->` was reaching
   the model inside the system prompt. A real defect, found by diffing rather than by symptom.
8. **`response_schema` is forwarded** as `text.format.json_schema` (strict=False) instead of the
   schema-less `json_object`. `_to_json_schema` folds Gemini-style uppercase types, and is a
   deliberate duplicate of the OpenAI helper — REQ-ARCH-23 forbids adapter→adapter imports and
   neither helper is domain logic worth promoting for two call sites.
9. **Vision enabled.** `input_image` is accepted on both grok-4.6 and grok-4.3 (minimum 8x8 px —
   a 1x1 probe returns `invalid_image`, which is what first revealed support). `CAPABILITIES.vision`
   was `False` purely because nobody had checked.
10. **`url_citation` annotations** are appended as a `*Sources:*` block, mirroring OpenAI. xAI also
    inlines its own markdown citations in the answer text; the block is the predictable one.
11. **`store=True` and `prompt_cache_key`** are sent, both accepted by xAI.

12. **`USER_TURN_SYSTEM_ANCHOR` is lifted into a `developer` item**, mirroring `OpenAIAdapter`.
    Role precedence on xAI is **developer > instructions > user** — measured 2026-08-15 with six
    runs per cell across both orderings. Left in the user turn, the anchor occupies the weakest
    channel the API offers.

    **Correction:** this decision record previously stated the opposite — that xAI reversed the
    precedence and that porting the extraction would demote the anchor. That claim came from a
    single run with a degenerate instruction and no ordering control, and it was wrong. The
    lesson generalises: a one-sample probe is not a measurement, and provider precedence is
    exactly the kind of claim that reads plausible while being backwards.

    `OpenAIAdapter`'s hand-written "PERSONALITY ANCHOR" block is still **not** ported — it was
    written against a specific OpenAI failure mode and has never been tested on Grok.

**Still absent:** no Files API on xAI, so non-image binaries cannot be forwarded and `upload_file`
still raises. Upstream `FileConversionService` converts those to text before they reach an adapter.

**Measured prompt overhead:** xAI prepends ~206 tokens (grok-4.6) / ~193 (grok-4.3) of its own
before anything we send — identical on chat/completions, so server-side. Contents undisclosed; the
model refuses to reproduce them. Declaring `web_search` pushes input from ~207 to ~3,900 tokens.

## Alternatives rejected

- **Dual transport** (chat/completions normally, `/v1/responses` only for grounding): two message
  converters and two response parsers, permanently. Rejected against the repo's maintainability-over-
  elegance bias — this is the more maintainable option, not the fancier one.
- **Dropping grounding and declaring `native_grounding=False`**: was the initial recommendation
  while Grok carried no traffic, and was overruled once the Responses API proved to support the
  full surface (text, `web_search`, `x_search`, function tools, JSON mode, `reasoning.effort`).

## Consequences

- Grok is functional in production for the first time. It is still **not a default for any agent** —
  it stays in `allowed_providers` for router/quick/smart and is reachable via user override or
  Smart's provider rotation. Promoting it needs an eval, as was done for `gpt-5.4-mini`.
- **Historical Grok cost data is meaningless** — there is none, because no call ever succeeded.
- **Not modelled:** xAI doubles both input and output pricing once a prompt reaches 200k tokens.
  Long-context Grok requests are under-costed 2x. Acceptable while Grok carries no default traffic.
- **Grounding is expensive.** One grounded query measured 17k input tokens across 5 server-side
  tool calls (~$0.04) versus ~$0.0003 for a plain call. Not a free bonus.
- The API key was rotated: the pre-fix key leaked into Cloud Logging during the investigation, when
  `httpx` embedded the offending header value in an exception message.

## Secret hygiene

An audit of every secret in the project found **8 ending in `\n`**: `XAI_API_KEY`,
`SLACK_BOT_TOKEN`, `SLACK_BOT_TOKEN_DEV`, `OAUTH_SESSION_SECRET`,
`MICROSOFT_TASKS_WEBHOOK_SECRET`, `MICROSOFT_TODO_CLIENT_ID`, `MICROSOFT_TODO_CLIENT_SECRET`,
`MICROSOFT_TODO_REDIRECT_URI`. Only `XAI_API_KEY` is proven broken; the others reach different
clients and were not verified. Create secrets with `printf %s ... | gcloud secrets versions add
--data-file=-`, never `echo`.

---

# Addendum, 2026-08-15: the first live briefing, and what it exposed

Making Grok work made it the *actual* provider for the first time, and the daily morning
briefing failed four times the next morning. The revival did not break anything — it
removed the failure that had been hiding three older defects.

## What the config had been doing

`complexity_settings_overrides.simple_analytics` had pointed at
`{tier: performance, provider: grok, thinking: medium}` for weeks. Every Grok call died
instantly on the trailing-newline bug at turn 1, before the transcript was
provider-locked, so cross-provider failover silently moved the run to OpenAI. BigQuery
shows the boundary exactly: Smart on `gpt-5.6-terra` through 08-13, Grok from 08-14, Grok
only on 08-15. The briefing had been running on a provider nobody chose.

## Three budgets, each too small, each masking the next

1. **Adapter client timeout 60s** while OpenAI's is 300s and Claude's `read` is 120s.
   Harmless for a dead adapter; fatal for a live one. Measured on the real workload,
   grok-4.6 at effort medium: 42s / 25s / 49s at 20–73k tokens, past 60s at ~100k. With
   `max_retries=2` each miss cost 3×60s, plus our own `llm_same_provider_retry` — up to
   360s for one turn of five. All three runs died at turn 5, deterministically.
   The adapter was also missing OpenAI's **per-request timeout forwarding**, so an
   explicit `LLMRequest.timeout` was clamped by the client ceiling anyway.

2. **Cloud Tasks `dispatch_deadline` never set.** `enqueue_worker_task` had no such
   parameter at any layer, so every worker task took the Cloud Tasks default of 600s.
   `NOTIFICATION_SLA[REMINDER]` promises 1500s at PERFORMANCE with the comment "Cloud Run
   cap −5 min" — that budget, and `DAILY_DIGEST`'s 1500s, had been unreachable since they
   were written. Now derived from the SLA table via `dispatch_deadline_s(kind)`.

3. **SLA tier resolved from the unmerged defaults.** `WorkerHandler` read
   `DEFAULT_COMPLEXITY_SETTINGS` directly while `TaskExecutionResolver` merged the user's
   overrides. Work ran at PERFORMANCE, the clock was set for BALANCED. The merge now lives
   in one place, `domain.complexity_settings.resolve_complexity_settings`.

Compounding: `execute_reminder` returns 500 on failure by design, so Cloud Tasks re-ran
the identical 10 minutes twice more, and the third failure opened Smart's circuit breaker
— taking down all Smart traffic, not just the briefing. ~90k grok-4.6 tokens per run, three
runs, nothing delivered.

**Residual risk, accepted:** 300s × 3 SDK attempts = 900s worst case for one LLM call
against a 1500s budget. OpenAI carries the identical exposure without incident. The fix if
it bites is a `request_timeout_s` for Smart, as `DOC_PLANNER` already does — which the
per-request forwarding above now makes possible.

## Mirror audit: what else had drifted from OpenAIAdapter

The timeout was found by asking the question the revival should have asked. Six more
answers, each verified rather than assumed:

- **`reasoning.effort` was forwarded raw.** OpenAI normalises unknown values to `medium`;
  Grok did not, and the value comes from user config. Probed live 2026-08-15: `low` /
  `medium` / `high` are accepted by both models; **`none` is accepted by grok-4.3 and
  returns 400 on grok-4.6**. Now normalised, with a model gate for `none`.
- **Grounding needs no forced effort** — the one OpenAI behaviour deliberately *not*
  mirrored. OpenAI forces `low` because gpt-5.4 defaults to `none`, which disables agentic
  search. Both xAI models reason by default and were measured running real agentic search
  with no `reasoning` block (grok-4.6: 1 `web_search_call`, grok-4.3: 2).
- **An enabled `cache_config` raised `ValueError`**, killing the whole agent execution over
  a hint the adapter can drop — and contradicting the `prompt_cache_key` it sent twenty
  lines later. Now ignored with a debug line.
- **`web_search_call` items were counted, not logged.** A grounded turn was a black box:
  no way to tell "searched and found nothing" from "never searched".
- **No empty-response warning** for a reply carrying neither text nor a tool call.
- **The `PERSONALITY ANCHOR` block is now ported.** Decision 12 above declined it as
  untested on Grok; that was overruled deliberately, since Smart-on-Grok was otherwise
  running without the reinforcement that fixed Smart's voice on OpenAI. Untested on Grok
  remains true — judge it on the first live briefing.

**Not mirrored, and correctly so:** the `json_object` branch omits OpenAI's
`"Respond in JSON."` developer item. It is unreachable on Grok — it requires
`response_mime_type` without a dict `response_schema`, and of the three agents allowed on
Grok, Router sends both (→ `json_schema`) while Smart and Quick send schema + tools
(→ the synthesized `deliver_response` tool).

**A comment corrected:** `MODEL_TIERS` claimed grok-4.3 has "no reasoning by default". It
reasons by default (83 reasoning tokens on a bare probe), as does grok-4.6 (66). Only
grok-4.3 can be told to stop.
