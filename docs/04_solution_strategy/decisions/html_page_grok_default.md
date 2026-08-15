# HtmlPageGenerator defaults to Grok

**Date:** 2026-08-15
**Status:** Implemented
**Scope:** `src/services/agent_context_builder.py` (`html_page` strategy),
`src/infrastructure/agent_config.py`, `src/agents/html_page_generator_agent.py`,
`scripts/html_page/ab_grok.py`

## Context

Grok became functional on 2026-08-14 (`grok_revival_2026_08.md`) and carried the daily
briefing's orchestration the next morning. The page generation hop stayed on
`gemini-pro-latest`. The question was whether it should move too.

## Measurement

`scripts/html_page/ab_grok.py` runs the real `HtmlPageGeneratorAgent` over a **fixed
input**: the actual `create_html_page` delegation Smart issued during the 2026-08-15
09:45 UTC briefing — 24,546 chars of assembled content, saved with the Gemini baseline to
`scripts/memory/html_page_input_2026-08-15.json`. The Gemini leg is not re-run; it is the
production run itself, so the comparison is against real behaviour rather than a replay.

| | gemini-pro-latest (prod) | grok-4.6 (bench) |
|---|---|---|
| cost | $0.1999 | **$0.1332** (0.67x) |
| wall time | 119.6s | **228.8s** (1.91x) |
| prompt tokens (uncached) | 11,366 | 10,630 |
| completion tokens | 14,768 | 18,652 |
| HTML size | 47,342 B | 46,161 B |

Grok spends 26% more output tokens for a marginally smaller page — more of the budget goes
to reasoning and internal verbosity than to the document.

## Decisions

1. **`default_provider: "grok"`, `fallback: "gemini"`, grok added to `allowed_providers`.**
   Quality was judged by the owner on the rendered output; the numbers above decided only
   that it is not a cost regression. **Latency is acceptable specifically because
   `create_html_page` is an ASYNC intent** — the page is delivered by a separate Cloud Task
   and nobody waits on it. This trade-off does not transfer to a synchronous agent.
   Gemini is the fallback because it is the previous default and the only provider with a
   track record on these pages; claude/openai remain in `allowed_providers` but have never
   generated one.

2. **`request_timeout_s = 420` on `HtmlPageGeneratorAgentConfig.`** At 229s measured
   against the adapter's 300s client ceiling the margin was 24%. Overrunning that ceiling
   does not fail fast: the SDK retries twice (`max_retries=2`), so up to three full
   generations are paid for and discarded before `timeout_ms` (600s) kills the agent with
   no page produced. `LLMRequest.timeout` is wrapped in `asyncio.wait_for` by the adapters,
   so this bounds TOTAL wall time including retries. Follows the `DOC_PLANNER` precedent;
   it is usable on Grok only because the per-request timeout forwarding was added the same
   day (previously the client ceiling clamped it).

3. **The user-level override was removed, not overwritten.** `agent_providers["html_page"]`
   was pinned to `"gemini"` and outranks the strategy default (`resolve_provider_name`
   priority: per-agent → global preference → strategy). Deleting the key lets the code
   default govern, so a future default change takes effect without another data edit.

## Probed, not assumed

- **Parameter envelope:** `temperature=1.2` accepted (xAI ceiling is "less than 2");
  `max_output_tokens=64000` accepted, as is 200000. No adapter change was needed.
- **Recitation:** `_call_llm_recitation_aware` exists because Gemini blocks
  newspaper-building prompts with an empty HTTP 200 (`6357b5f`, which touched this agent).
  `GrokAdapter` has no `finish_reason` mapping, so that retry can never fire on Grok. Two
  verbatim-reproduction probes came back `status='completed'` with refusal **text** — the
  dangerous silent-empty mode does not reproduce on xAI, so there is nothing to retry.
- **Rate limits:** `x-ratelimit-limit-requests: 7200`, `x-ratelimit-limit-tokens: 50,000,000`,
  both fully remaining. A briefing spends 68 calls total. Not a constraint.
- **`allowed_providers` is not a gate on the bench path.** `_build` reads only
  `strategy["fallback"]`; `allowed_providers` is consulted by `resolve_provider_name` and
  `resolve_next_provider`. So `provider_override` could pin Grok for measurement *before*
  any production change — measure first, decide second.

## Known gaps, accepted

- **A refusal is published as a page.** Grok refuses in text; the agent's guard is
  `if not html_code`, which a 64-char refusal passes. On Gemini the same class of event
  produced an empty response that was caught, retried and surfaced as a failure. Low
  probability — the briefing prompt asks to compose and verify, never to reproduce — but
  the shape changed from silent-empty to silent-garbage.
- **Truncation is undetected.** xAI *does* report it
  (`status='incomplete'`, `incomplete_details.reason='max_output_tokens'`), and mapping it
  into `finish_reason` would be straightforward. It would not fix this on its own:
  a truncated page is non-empty, so it passes the same guard. Detecting truncation on
  non-empty output is a separate change and was not made.
- **N=1.** `temperature=1.2` makes output high-variance. The cost figure is the right order
  of magnitude, not a third-decimal number, and the size/placeholder differences between the
  two legs are within noise.
- **xAI's 200k pricing cliff is still unmodelled** (`billing.py`). This prompt is ~10.6k, so
  it does not bite here.
