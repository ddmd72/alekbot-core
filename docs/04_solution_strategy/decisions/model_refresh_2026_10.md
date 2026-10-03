# Model refresh 2026-10-03: Claude 5.5, GPT-6, Grok 4.7, top model on ULTRA

**Status:** accepted and deployed 2026-10-03 (revision alek-bot-dev-00355; previous 00354). Prices are recorded in `price_refresh_2026_10.md`.

## Decision

| Tier | Claude | OpenAI | Gemini | Grok |
|---|---|---|---|---|
| ECO | haiku-4-5 | **gpt-6-luna** | 3.5-flash-lite | 4.3 |
| BALANCED | haiku-4-5 | **gpt-6-luna** | **3.8-flash** (pinned) | 4.3 |
| PERFORMANCE | **sonnet-5-5** | **gpt-6.1-sol** | pro-latest (3.1-pro) | **4.7** |
| ULTRA | **fable-5-1** | **gpt-6-astra** | pro-latest (3.1-pro) | **4.7** |

ULTRA is the top model of each provider. Nothing used ULTRA in the 14 days before the change.

## Evidence

- **Consolidation:** `scripts/consolidation/ab_cross_provider.py`, 2 runs each on a real window.
  Sonnet 5.5 vs Sonnet 5 extracted the same core facts. 5.5 ran 55–58s against 113–132s and cost
  $0.17–0.26 against $0.27–0.39. On both runs 5.5 also captured the user's "search exhaustively"
  feedback as a directive; Sonnet 5 captured it on neither.
- **OpenAI:** `scripts/validation/ab_agent_models.py` runs the real agent on real queries with a
  blind Claude judge. Smart runs dry: read-only delegations are real, writes are stubbed.
  - Smart, gpt-6-luna vs 5.6-luna: **7:1**, 37% cheaper.
  - search_web: 6:2, 40% cheaper.
  - fetch_url vs nano: 4:4, 56% cheaper.
  - Maps geo suite (`ab_maps_models.py --fresh`): on par, no fabrication.
  - Smart PERFORMANCE, gpt-6.1-sol vs 5.6-terra: 5:3 in sol's favour, but p50 56s vs 35s and a
    544s worst case, at 32% more cost.
- Smart ULTRA, gpt-6-astra vs gpt-6.1-sol (6 queries): 2:3 with 1 tie, p50 37s vs 39s, **5.2× the cost**.
  Astra is on ULTRA by owner choice (top model per provider, for consistency), not because it won.
- **Grok 4.7** at effort medium on tool turns: p50 1.6s vs 3.8s, and 2–3× fewer output tokens.
- **Gemini:** the only change was the BALANCED alias moving to 3.8-flash on 2026-09-02, which
  nobody decided. That tier is now pinned.

## Traps closed in the same change (each reproduced as a live 400 first)

- **Claude.** Sonnet 5.5 rejects `thinking: disabled` and needs `between_tools`. The 5.5 and
  Fable 5.1 generation rejects forced `tool_choice`. Opus 5.x rejects `temperature`. The id
  substring `claude-sonnet-5` also matches `-5-5`, which is how these slipped through.
- **Claude refusals.** These arrive as HTTP 200 with `stop_reason: "refusal"` and used to pass
  through as an empty success. They are now raised as `LLMClientError`.
- **GPT-6.** It rejects `temperature`. `gpt-6.1-sol` rejects `effort: none`. `gpt-6-luna`
  reasons by default, so the adapter sends `effort: none` when no thinking is requested.
- **`xhigh`.** It used to collapse to `medium` on OpenAI and Grok and to `LOW` on Gemini.
- **Claude rollback default.** `cloudbuild-dev.yaml` pinned `CLAUDE_PERFORMANCE_MODEL=claude-sonnet-5`,
  so a code-only tier flip would never have reached production.

## Rollback without a redeploy

- Claude: `make claude-rollback` sets Sonnet 5.
- OpenAI: `make openai-rollback` restores the GPT-5.x map via `OPENAI_TIER_OVERRIDES`.
- Grok and Gemini: revert the tier line and redeploy.

## Rejected

- *BALANCED on gpt-6.1-sol.* It costs 20× luna, and luna already beats the previous BALANCED model.
- *Keeping maps on 5.6-luna.* On event queries from the fan-out it gave more Maps detail (2:6), but
  on the geo suite the two were on par. A per-agent model pin does not exist and is not worth adding
  for this.

## Revisit when

- the PERFORMANCE latency on gpt-6.1-sol hurts in daily use. Try effort `low` before changing the model.
- refusals show up in alerts on routine traffic.
