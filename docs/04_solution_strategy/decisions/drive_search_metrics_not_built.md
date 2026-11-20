# Decision: the drive search metrics pipeline is not built

**Status:** Adopted
**Date:** 2026-11-06

## Context

A design of 2026-10-14 (the RFC `DRIVE_SEARCH_METRICS_RFC.md`, deleted; its facts worth keeping are below) designed a metrics pipeline for drive search: a per-search event
table, per-turn facts derived from Smart's tool calls, about thirty versioned aggregates, a daily rollup, a weekly Slack
report and an LLM judge. Its own estimate is roughly two weeks of work, with about a dozen domain modules, three ports,
five tables and two scheduled tasks.

Since then the measured path changed: Smart no longer calls the drive search, list and open tools itself — it commissions
the drive specialist through `work_with_drive_files` (`src/infrastructure/agent_manifest.py`), and search gained passages
(`find_passages` / `read_passages`), several drives per user and Google Drive. The design covers none of that. It also
notes that, at a few users' traffic, weekly deltas below about ten percentage points are noise.

## Decision

The pipeline is not built; the RFC is closed and deleted. Search quality is judged by a fixed, on-demand evaluation set instead:
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

## Facts for whoever reopens it

Carried over from the deleted RFC:

- **Join key:** the `[DriveSearch]` log line's Cloud trace id equals `prompt_content.trace_id` without its `tr_` prefix and
  equals the Logfire trace id; every join between the three sources goes through it.
- **Why BigQuery** for the tables (metrics computed in Python by worker tasks): Firestore has no SQL, no percentiles and
  no join to `prompt_content`; BigQuery scheduled queries are SQL outside git with no tests; Logfire keeps at most 90 days.
- **BigQuery traps:** DML cannot touch rows still in the streaming buffer, so an idempotent rewrite uses a load job with
  `WRITE_TRUNCATE` per partition; a join across datasets in different locations fails.
- **Release pooling** keys on a `search_build` fingerprint of the search code and parameters, not on the Cloud Run
  revision (most revisions do not change search).
- **The judge** is Claude, pinned (exact model and effort), a different family from the one that wrote the cards it
  grades (profiles and passage hints come from an OpenAI model); it runs on the owner's searches only, because the
  indexing disclosure covers describing files, not grading searches; its provider strategy allows `claude` and has no
  fallback.
