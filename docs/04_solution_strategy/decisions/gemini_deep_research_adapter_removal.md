# Decision: Remove GeminiDeepResearchAdapter

**Status:** Adopted
**Date:** 2026-05-29
**Context:** Google I/O 2026 introduced breaking changes to the Gemini Interactions API (`outputs` → `steps`, polymorphic `response_format`); legacy schema is removed 2026-06-08, Python `google-genai` SDK ≥ 2.0.0 auto-opts into the new shape. Our `GeminiDeepResearchAdapter` is the only code that touches `interactions.create` / `interactions.get` — every other Gemini call site uses `models.generate_content`, which is unaffected.

## Decision

Delete `GeminiDeepResearchAdapter` and its wiring. Bump `google-genai>=2.0.0,<3.0.0`. Keep Claude (default) and OpenAI deep-research providers; remove `"gemini"` from `deep_research.allowed_providers`.

## Why now

- **Zero production usage.** Log search (dev + prod, 30-day window, `[DeepResearch][gemini]` marker) returned no entries — no user has set `agent_providers["deep_research"] = "gemini"` since the multi-provider split landed. The adapter is a speculative seam.
- **External deadline.** Interactions API legacy schema is removed 2026-06-08 (10 days). Keeping the adapter forces a v2 migration on a code path that was never exercised.
- **SDK pin gates everything else.** `google-genai>=1.72.0` (unbounded) was the actual risk for the rest of the Gemini surface. Removing the Interactions API call site lets us pin to ≥ 2.0.0 without migration work — the Models API surface we use (`generate_content`, `generate_content_stream`, `files.upload`, `embed_content`) is unaffected by the v2 cut.

## Removal scope

- `src/adapters/gemini_deep_research_adapter.py` — file delete.
- `tests/unit/adapters/test_gemini_deep_research_adapter.py` — file delete (per-test permission granted 2026-05-29).
- `scripts/debug/test_gemini_deep_research.py` — file delete (per-file permission granted 2026-05-29).
- `main.py` — drop import + `job_registry.register("gemini", …)` block + `GEMINI_DEEP_RESEARCH_MODEL` env read.
- `src/services/agent_context_builder.py` — remove `"gemini"` from `deep_research.allowed_providers`.
- `requirements.txt` — `google-genai>=2.0.0,<3.0.0`.
- Doc sweep: `STRUCTURE.md`, `TARGET_ARCHITECTURE.md`, `DEEP_RESEARCH_RFC.md`, `agent_registry/README.md`, `multi_agent_system/README.md`, `openai_integration/README.md`, CLAUDE.md, port + agent docstrings.

## Rejected alternatives

- **Migrate the adapter to v2 schema (steps[] reader, SDK ≥ 2.0.0).** Real engineering cost (rewrite + tests + decision record) on a code path with zero usage. Speculative seam — violates YAGNI and `feedback_clean_or_explain.md`.
- **Pin `google-genai<2.0.0` and keep 1.x semantics.** Third lane. Buys until 2026-06-08, then Interactions API breaks anyway on 1.x. Also blocks adopting any future 2.x improvements across the rest of the Gemini surface (Models API, embeddings).
- **Keep the adapter but stub `create_interaction()` to raise `NotImplementedError`.** Dead code still in graph, still requires SDK pin discussion. No advantage over deletion.

## Triggers to revisit

- A concrete user requirement to A/B Gemini deep research against Claude (`deep-research-pro-preview-12-2025` is the December 2025 model; the April 2026 successors `deep-research-preview-04-2026` / `deep-research-max-preview-04-2026` add MCP, File Search, collaborative planning — re-adding would target one of those, not the December model).
- Claude deep-research runner reliability degrades and we need a second non-OpenAI provider.
- Re-introduction targets the v2 Interactions schema natively — no porting of legacy code.

## Related

- `cloud_tasks_vs_jobs.md` — Claude DeepResearch runs as a Cloud Run Job; gemini path was Cloud Tasks polling. Removal does not affect the Claude path.
- `feedback_clean_or_explain.md` — clean removal over deferred half-migration.
