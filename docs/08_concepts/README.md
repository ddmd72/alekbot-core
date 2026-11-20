# 08 Concepts

Cross-cutting ideas that more than one building block relies on.

- **[Multi-Vector RRF Search](./multi_vector_rrf_search.md)** — why memory search fuses ranks from several vectors,
  and how duplicates are judged.
- **[Groovy Prompt Pattern](./groovy_prompt_pattern.md)** — why prompts are written as a Groovy-style class, and how
  to write a token.
- **[Prompt Design System v3](../05_building_blocks/prompt_design_system_v3/README.md)** — how tokens, profiles and runtime context become one
  system prompt, and where the cache boundary sits.
- **[Firestore Collections](./DATABASE_SCHEMA.md)** — every collection, its document id, and where its shape is
  defined.

Per-component detail lives in `../05_building_blocks/`. The rules every session loads are in the root `CLAUDE.md` and
in `src/*/CLAUDE.md`.
