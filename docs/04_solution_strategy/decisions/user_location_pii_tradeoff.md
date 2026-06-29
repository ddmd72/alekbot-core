# Decision (DEFERRED): user location is full-address free text in every prompt

**Date:** 2026-06-21
**Status:** Deferred (documented trade-off, no change made)

## Context

`UserBotConfig.location` is a free-text field and in practice holds a **full street address**
(e.g. a value like `Carrer d'Exemple, 1, 00000 Vila, Region` — real values are user PII, never
committed). It is injected into the `knowledge_base { user_location: '...' }` block of **every**
agent's system prompt — not just the geo-aware ones — so it is sent to all four LLM providers
(Gemini/Claude/OpenAI/Grok) on essentially every request, and lands in their prompt caches.

## The trade-off (why this isn't a clear-cut fix)

- **Precision genuinely helps geo agents.** Verified in BigQuery + Cloud Run logs 2026-06-21: on a
  weather query the LLM organically injected the locality/neighbourhood into its `search_web` query
  strings, producing hyperlocal results — better than a bare city name.
- **But it is broad-surface PII** for precision only ~2–3 of ~20 agents actually need. The full
  address reaches every provider and every cache on every turn.

## Options (not yet chosen)

- Scope location to only the agents that need it (geo/web/maps) instead of the global
  `knowledge_base`.
- Store a coarse locality (city/neighbourhood) for the global block; keep the full address behind a
  dedicated geo-only field.
- Leave as-is (status quo) — accept the PII surface for the precision.

## Decision

**Deferred.** No change made; the trade-off is recorded so a future revisit is a deliberate choice,
not a rediscovery. Revisit if the privacy surface becomes a concern or a multi-user deployment
changes the PII calculus.
