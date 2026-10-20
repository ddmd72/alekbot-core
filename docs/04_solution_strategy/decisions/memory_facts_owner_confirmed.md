# Memory facts: the bot proposes, the owner confirms

**Date:** 2026-10-19
**Status:** Implemented on `feat/memory-propose-facts`. RFC: `docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md`.

## Decision

The bot never writes to memory. Smart's local tool `propose_memory_facts` (interactive turns only, at most 20
facts) stores a 7-day draft; the owner reads it as a file and pastes `$memory save <code>`; `MemoryFactService.confirm`
appends a user/model pair to the origin session, and background consolidation does the rest. The `save_to_memory`
intent and every writer of `consolidation_text` are removed.

## Why

The consolidator reads user messages as the owner's own words. `save_to_memory` put bot-written text there, so
procedures became standing directives and file notes became facts. Provenance is only true when the owner causes the
message.

## Alternatives rejected

- **Keep `save_to_memory` and label submissions for the consolidator.** The bot still decides the content, and nothing in code enforces "only on my request".
- **Quotas.** They bound volume, not content: 20 injected facts still get written.
- **A pending store with an hourly flush.** A second write path into consolidation and more Stage 2b runs, for a wait the history pair already shows.
- **Direct fact writes.** They bypass consolidation as the single curator (SCD2, dedup).
- **Detecting "remember" in the owner's text.** Brittle across uk/ru/en/es, and the bot still picks the content.

## Revise when

- Few proposals get confirmed (the owner skips the paste step, or finds it too heavy).
- The echo trap discards confirmed facts in practice (RFC §8 dry run, then production batches).
- The pair leaves Smart's 30-message view before consolidation often enough to cause "saved but not found" answers.
- No stored session carries `consolidation_text` any more: remove the field and its readers.
