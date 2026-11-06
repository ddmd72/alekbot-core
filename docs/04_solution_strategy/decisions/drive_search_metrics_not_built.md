# Decision: the drive search metrics pipeline is not built

**Status:** Adopted
**Date:** 2026-11-06

## Context

`docs/10_rfcs/DRIVE_SEARCH_METRICS_RFC.md` (2026-10-14) designs a metrics pipeline for drive search: a per-search event
table, per-turn facts derived from Smart's tool calls, about thirty versioned aggregates, a daily rollup, a weekly Slack
report and an LLM judge. Its own estimate is roughly two weeks of work, with about a dozen domain modules, three ports,
five tables and two scheduled tasks.

Since then the measured path changed: Smart no longer calls the drive search, list and open tools itself — it commissions
the drive specialist through `work_with_drive_files` (`src/infrastructure/agent_manifest.py`), and search gained passages
(`find_passages` / `read_passages`), several drives per user and Google Drive. The RFC covers none of that. The RFC itself
notes that, at single-user traffic, weekly deltas below about ten percentage points are noise.

## Decision

The pipeline is not built; the RFC is closed. Search quality is judged by a fixed, on-demand evaluation set instead:
queries with their expected files (bare keys, named files, and queries whose right answer is "nothing"), run through the
existing runner in `scripts/validation/drive_eval/` before and after a ranking or indexing change. The set is built when
the first change that needs it is worked (passage ranking for bare keys, the `Z_RELATED` threshold in
`src/domain/drive_index_params.py`); it uses real data, so it stays outside the repo.

## Rejected

- **Build the RFC as written:** it measures a path that no longer exists, and its permanent tables, parser and judge
  series are the largest silent-failure surface for the least decision value at this traffic.
- **A reduced pipeline (events only):** logs, BigQuery `prompt_content` and Logfire already answer "what did this search
  return"; trends need volume the system does not have.

## Triggers to reopen

More than one active user with regular drive use, or a search regression the logs and the evaluation set cannot explain.
