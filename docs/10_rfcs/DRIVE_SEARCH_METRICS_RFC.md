# RFC: User Drive search metrics

**Status:** Proposed, revision 1 (2026-10-14). Nothing is built or active. The owner approved the nine decisions of
the study (§3) on 2026-10-14; this text turns them into a design. **S0 (§12) is time-critical: it must run before
2026-11-09 00:00 UTC** or the only pre-v2 baseline expires.
**Builds on:** [USER_DRIVE_INDEXING_RFC.md](USER_DRIVE_INDEXING_RFC.md) (Search v2, §25: tiers, coverage line, D29),
the drive building block [user_drive/README.md](../05_building_blocks/user_drive/README.md) (§6 search, §7 operations).
**Precedents reused:** `PromptContentStore` / `BigQueryPromptContentAdapter` (best-effort hot-path capture into a
30-day BigQuery table), `AlertSinkPort` / `SlackWebhookAdapter` (the billing summary), the drive model runners in
`composition/` (`DriveReaderRunner`: an agent behind a port), `WorkerHandler` scheduler tasks, `TokenLedger` /
`calculate_cost` in `domain/billing.py`, the offline drive eval fixture (`tests/fixtures/drive_eval/`).

All names of projects, datasets, services, users and URLs are given here as environment-variable names. Their values
live in `.env` and Secret Manager (CLAUDE.md, SECRETS RULE).

---

## 1. Problem

Search v2 makes no model call at search time and returns files in tiers (About, Mentioned in, By translation,
Labelled, Related), with a coverage line and the D29 "Without '<word>'" line. Its offline evals (G1, G2, G3, the
Search v2 eval) guard a synthetic fixture. Nothing measures it on real drives.

What we can see today:
- **One `[DriveSearch]` log line per search** (counts only, no query) in Cloud Logging, 30 days. It is not in Logfire.
- **Smart's tool calls** in the `prompt_content` BigQuery table (30 days): `delegate_to_specialist` with
  `args.intent`, so a turn's searches, opens and listings can be counted after the fact.
- **The trace id joins all three sources:** the log line's Cloud trace id equals the `tr_`-prefixed `trace_id` in
  `prompt_content` and the Logfire trace id.

What we cannot answer:
- Did the last deploy make search better or worse, and on which kind of query?
- How often does Smart crawl the drive (many listings after a search) instead of trusting the index? How often does
  it open a file the search already showed as About? What does it do after a D29 line?
- Are the results right? There are no labels, and the users will not label.
- Is the indexer keeping up, and what does it cost per file?
- Any of the above for a period older than 30 days: every source expires.

Two incidents motivate the design. The "Barcelona" turn of 2026-10-13 made 8 LLM calls, 8 searches and 9 listings
for 371k tokens; we found it by reading logs by hand. And every v2 search since the rebuild reports
`coverage=incomplete` because enrichment is still running — a single "incomplete rate" would hide every other cause.

## 2. Goals and non-goals

**Goals**
- G1. Per-search and per-turn facts, captured for every search and every drive turn, joinable by trace id.
- G2. Metrics as **code**: pure, unit-tested, versioned definitions in `src/domain/`, covered by `make check`.
- G3. **Permanent aggregates** that serve any future window (week, month, release, all time) without the raw events.
- G4. A **regression view**: the current search build against the previous one, plus a weekly trend.
- G5. **Quality without labels**: an LLM judge on a weekly sample, checked against anchors with known answers.
- G6. **Indexer health and cost** per user and day.
- G7. A weekly report in a dedicated Slack channel, with outliers that link to their trace.
- G8. Privacy at least as strict as `prompt_content`: text lives 30 days; permanent rows hold no text and no raw id.

**Non-goals**
- Online experimentation (A/B splits by user): 3 users make it meaningless.
- Alerting on metric thresholds: the report is weekly and human-read. Operational alerts stay where they are.
- A dashboard. Looker Studio on the BigQuery tables is possible later and needs nothing from this design.
- Metrics for memory search, web search or email search. The row model is generic; the catalogue is not.
- User feedback (👍/👎) — S6, deferred (decision 9).
- Changing search behaviour. This RFC observes; it changes no ranking, tier or prompt.

## 3. Decisions (owner, 2026-10-14)

| # | Decision | Reason |
|---|---|---|
| 1 | **Storage: BigQuery** for detail (30-day TTL) and permanent aggregates; metrics computed **in Python** by worker tasks. | Firestore has no SQL, no percentiles and no join to `prompt_content`. BigQuery scheduled queries are SQL outside git with no tests. Logfire keeps at most 90 days. Cost here is ≈ $0 (streaming at cents per 200 MiB, storage free under 10 GiB, queries free under 1 TiB/month; load jobs free). |
| 2 | **Identity in permanent rows: `user_hash` = HMAC-SHA256(key, user_id), first 16 hex**, key in Secret Manager, plus **all-user rows** (`user_hash = "*"`). | With 3 users a hash hides no one from the owner. Its value is that a leaked permanent table cannot be joined to Firestore or to anything else without the key. All-user rows keep the totals when a user's rows are erased and save the report a pass over every user. |
| 3 | **Turn data derived from `prompt_content`** (Smart's `tool_calls`), not emitted by Smart. **Re-decide if more than 1 % of rows are unparsable** (§5.2 `parse_ok`). | No hot-path change in Smart, and it can be backfilled from 2026-10-10. The cost: coupling to how `tool_calls` are serialized, and failed LLM calls have no row (CLAUDE.md, Debugging). A Smart-side `ContextVar` tally (like `TokenLedger`) would be precise but has no history. |
| 4 | **Judge = Claude Sonnet 5.5, pinned** (exact model id and effort), **on the owner's searches only**. | The indexing disclosure covers describing files, not grading searches. Sonnet avoids grading cards that its own family wrote (profiles and passage hints come from GPT-6 Luna by default). Other users need an opt-in and a disclosure first (§9). |
| 5 | **Comparison unit: release against release** as the primary view, the weekly trend second. | At 5–26 searches a day a weekly delta under ~10 pp is noise; a release pools more n and is what a regression is attributed to. §6.4 defines "release" so that a docs-only deploy does not split a pool. |
| 6 | **Result text allowed in the 30-day event table** (`rendered_result`, which can hold document passages). | Same data class and TTL as `prompt_content`, which already holds it inside Smart's prompt. The judge and outlier review need it. |
| 7 | **S0 baseline copy before 2026-11-09** (planned; not executed until the owner runs it). | The drive turns start on 2026-10-10. That partition of `prompt_content`, and the log lines of those days, expire about 2026-11-09. It is the only pre-v2 baseline. |
| 8 | **Report via a dedicated Slack webhook** from Secret Manager. | A bot DM would enter chat history and consolidation; the ops webhook would mix a weekly review into alerts. |
| 9 | **👍/👎 feedback (S6) after 4 weeks of judge runs.** | Its labels then serve mainly to check the judge, and it needs a `ts → trace_id` map and a Slack event subscription. |

## 4. Architecture

```
HOT PATH (main service)
  FileManagementAgent ─ search_files_in_drive ─► DriveSearchService.search()
                                                    │  builds DriveSearchEvent (domain) beside the [DriveSearch] line
                                                    ▼
                                    DriveTelemetrySink (port) ── record_search(event): best-effort, never raises
                                                    │
                                    BigQueryDriveTelemetryAdapter: fills trace/span ids from the telemetry
                                    context, streams one row into drive_search_events (30 d)

DAILY (Cloud Scheduler → main /worker task_type=drive_metrics_rollup, day = yesterday UTC)
  WorkerHandler ─► DriveMetricsService.rollup_day(day)
      DriveMetricsStore.turn_rows(day)         ── prompt_content rows of drive traces, tool_calls parsed (adapter)
      DriveMetricsStore.search_events(day)
      domain: derive_turn_facts(...)  → replace_turn_facts(day)          drive_turn_facts (30 d)
      DriveMetricsService.snapshot_index(day)  (S4)                      drive_index_daily (permanent)
      domain: METRICS registry → MetricRow per (metric, version, user_hash | "*", dims)
      DriveMetricsStore.replace_day_metrics(day, rows)                   drive_metrics (permanent)

WEEKLY (Cloud Scheduler → start_drive_metrics_weekly → Cloud Task drive_metrics_weekly, named by ISO week)
  WorkerHandler ─► DriveSearchReviewService.run_week(week)
      sample (domain, seeded by week) → QualityJudgePort.judge(item)    (S5; owner-opted users only, + anchors)
      DriveMetricsStore.replace_judge_verdicts(week, ...)                drive_judge_verdicts (30 d)
      rollup(day) for the week's 7 days   (re-run: judge metrics land in drive_metrics; one writer)
      DriveMetricsStore.day_metrics(...) → domain: render_report(...) → AlertSinkPort (dedicated webhook)
      DriveMetricsStore.mark_report_posted(week)                         drive_metrics_reports (permanent)

INDEXER (S4): the tick adds each settled record's (now − dirty_since) to a per-day histogram on DriveSyncState.
```

**Hexagonal placement** (CLAUDE.md, Layer Semantics and Import Rules):

| Layer | New | Notes |
|---|---|---|
| `domain/` | `drive_search_event.py` (event), `drive_coverage_reason.py`, `drive_turn_row.py` (parsed `prompt_content` rows and delegations), `drive_turn_facts.py` (turn facts, derivation), `drive_metric_row.py` (row, histogram, merge, quantile, intervals), `drive_metrics.py` (metric definitions and registry), `drive_index_snapshot.py`, `drive_settle_lag.py`, `drive_judge.py` (judge item, sampling, anchor agreement, `JUDGE_*` pins), `drive_judge_verdict.py`, `drive_metrics_report.py` (report model and text), `metrics_identity.py` (`user_hash`), `search_build.py` (fingerprint), `metrics_errors.py` | Pure: stdlib + pydantic. `hmac`/`hashlib` are stdlib. `mypy --strict`. |
| `ports/` | `DriveTelemetrySink`, `DriveMetricsStore`, `QualityJudgePort` | One per boundary: the hot-path sink and the rollup store are different contracts (best-effort fire-and-forget vs awaited, raising). The judge is an LLM boundary with a substitute in tests. |
| `adapters/` | `bigquery_drive_telemetry_adapter.py`, `bigquery_drive_metrics_store.py`, `bigquery_tool_calls.py` (the `tool_calls` parser); in-memory twins live in test helpers (`tests/unit/services/drive_metrics_fakes.py`, the `drive_index_fakes.py` precedent) | BigQuery SDK only here. The store parses `tool_calls` (storage-shaped work). |
| `services/` | `drive_metrics_service.py` (rollup, snapshot), `drive_search_review_service.py` (weekly: sample, judge, report) | No service imports another (REQ-ARCH-22): the review service receives the rollup as an injected callable and the index-status reader likewise. |
| `agents/` | `drive_search_judge_agent.py` (`DriveSearchJudgeAgent(BaseAgent)`, agent type `drive_judge`) | Internal: no intent, no manifest descriptor — like `drive_reader`. |
| `composition/` | `drive_judge_runner.py` (`DriveJudgeRunner(QualityJudgePort)`), `drive_metrics_setup.py` (wiring, build fingerprint) | The `DriveReaderRunner` precedent: composition builds the agent and adapts it to the port. |
| `handlers/` | `WorkerHandler` gains `drive_metrics_rollup`, `start_drive_metrics_weekly`, `drive_metrics_weekly` | Delegates only. |

**Why two ports at the BigQuery boundary, not one.** `DriveTelemetrySink` is the `PromptContentStore` contract:
called inside a user's turn, must not raise, must not block, may drop. `DriveMetricsStore` is called by a scheduled
task that must fail loudly and be retried (5xx) when BigQuery is down. One port with both contracts would make every
caller guess which rule applies.

**Why the tool-call parse sits in the store adapter.** `tool_calls` is a JSON string whose shape is decided by
`BigQueryPromptContentAdapter._build_record`. Parsing it is reading a storage format; the domain receives typed
`TurnRow` values with a `parse_ok` flag. The parser is a static function with a fixture test pinned to the writer's
format (§11 risk 2).

## 5. Data model

Five tables in one dataset, named by `DRIVE_METRICS_DATASET` (deploy-only `.env` key, passed as its own Cloud Build
substitution — CLAUDE.md, deploy-substitution trap). The dataset is created in the **same location as the
`prompt_content` dataset**, because a query that joins tables across locations fails. Detail tables are
DAY-partitioned with a 30-day partition expiration in code, as `prompt_content`; permanent tables are DAY-partitioned
without expiration. `schema_version` on every row; columns are only ever added.

### 5.1 `drive_search_events` (30 days; partition `DATE(ts)`, cluster `user_id, trace_id`)

One row per call of `DriveSearchService.search`, written beside the `[DriveSearch]` line. Streaming insert,
best-effort.

| Group | Field | Type | Meaning |
|---|---|---|---|
| Identity | `event_id` | STRING | uuid4 |
| | `ts` | TIMESTAMP | search start |
| | `trace_id`, `span_id` | STRING | from the telemetry context, same form as `prompt_content.trace_id` |
| | `user_id` | STRING | raw id, 30 days only (as `prompt_content`) |
| Versions | `schema_version` | INT64 | event schema, starts at 1 |
| | `release` | STRING | the Cloud Run revision (`K_REVISION`; `local` outside Cloud Run) |
| | `search_build` | STRING | fingerprint of the search path (§6.4) |
| | `keys_version`, `policy_version` | INT64 | `KEYS_VERSION`, `POLICY_VERSION` |
| | `params_hash` | STRING | 12 hex of sha256 over `drive_index_params`' public constants |
| Request | `status` | STRING | `ok`, `disabled`, `disconnected`, `folder_not_found`, `ambiguous_folder` |
| | `mode` | STRING | `query`, `filter`, `awaiting_rebuild` |
| | `scope` | STRING | `_Stats.scope` |
| | `content` | STRING | `off`, `wide`, `memory` |
| | `kind` | STRING | the kind filter, or null |
| | `has_folder`, `has_date_from`, `has_date_to` | BOOL | |
| Concepts | `concepts`, `numbers`, `ignored`, `common`, `absent`, `absent_in_scope`, `cut` | INT64 | counts from `TieredResult` (`numbers`: concepts of the number family) |
| Tiers | `about_n`, `mentioned_n`, `translated_n`, `labelled_n`, `related_n` | INT64 | tier totals |
| | `about_shown`, `mentioned_shown`, `translated_shown`, `labelled_shown`, `related_shown` | INT64 | cards shown per tier |
| | `page_full` | BOOL | a candidate page was full (`at_least`) |
| | `d29_line` | BOOL | a "Without '<word>': N files" line was rendered |
| | `d29_without_n` | INT64 | its N, or null |
| | `conjunction_n`, `expired` | INT64 | empty-conjunction pairs, expired files hidden |
| Coverage | `coverage_complete` | BOOL | `CoverageFacts() == coverage` |
| | `coverage_reasons` | ARRAY<STRING> | enum, one per non-empty `CoverageFacts` field: `not_described`, `not_describable`, `no_passages`, `not_read_yet`, `unread`, `building`, `reading`, `refreshing`, `rekeying`, `full_page`, `keys_cut`, `cut`, `names_only`, `unchecked` |
| | `building`, `reading` | BOOL | envelope flags |
| | `index_age_s` | INT64 | `ts − envelope.index_as_of`, or null |
| Streams | `card_n`, `chunk_n`, `hint_n`, `name_n` | INT64 | stream hit counts |
| | `name_queries`, `about_queries`, `word_queries`, `hint_queries`, `tested` | INT64 | query and candidate counts |
| | `pool_card`, `pool_chunk`, `pool_hint` | STRING | meaning pools (`-` when abstained) |
| | `band`, `visible`, `filtered`, `collapsed` | INT64 | |
| | `chars`, `returned`, `ms` | INT64 | rendered length, cards returned, wall time |
| Text (30 d) | `drive_query` | STRING | `request.query` |
| | `concept_forms`, `absent_forms`, `ignored_forms`, `common_forms` | ARRAY<STRING> | the words behind the counts |
| | `rendered_result` | STRING | exactly what Smart read (`render_results(result)`, already computed for `chars`) |
| | `shown` | ARRAY<STRUCT<tier STRING, item_id STRING, rank INT64>> | cards in display order |

### 5.2 `drive_turn_facts` (30 days; partition `day`)

One row per **drive turn**: a trace in which Smart delegated at least one of `search_files_in_drive`,
`list_files_in_drive`, `open_file_from_drive`. Built by the rollup from all `prompt_content` rows of the trace (router,
Smart and any specialist that called a model) joined to that trace's events. A turn belongs to the UTC day of its first
row.

| Field | Type | Meaning |
|---|---|---|
| `day`, `trace_id`, `user_id` | DATE, STRING, STRING | raw id, 30 days |
| `release`, `search_build` | STRING | from the trace's first event; null when the turn made no search |
| `facts_version` | INT64 | version of `derive_turn_facts` |
| `llm_calls` | INT64 | rows in the trace |
| `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_creation_tokens` | INT64 | sums |
| `cost_usd` | FLOAT64 | Σ `calculate_cost(row.model, …)` — priced per model that ran |
| `wall_s` | FLOAT64 | last row − first row timestamp |
| `search_calls`, `search_events` | INT64 | delegations vs events in the trace (their gap is a data-health signal) |
| `opens`, `lists`, `mutations`, `other_intents` | INT64 | delegations by intent family |
| `listings_after_search` | INT64 | listings after the first search |
| `opens_of_about_shown` | INT64 | opens of a file an earlier search in the trace showed in About |
| `after_d29` | STRING | after the first search with a D29 line, the next drive call: `dropped_word` (a search whose concepts lack the absent word), `other_research` (another search), `opened`, `listed`, `stopped` (none); null without D29 |
| `redundant_lang_researches` | INT64 | later searches whose concepts are in another script (Latin ↔ Cyrillic) and add no About/Mentioned file the earlier searches had not shown |
| `parse_ok` | BOOL | every Smart row's `tool_calls` parsed |
| `unparsable_rows` | INT64 | |

### 5.3 `drive_index_daily` (permanent; partition `day`)

One row per user with a sync state, written by the rollup **for yesterday only** (it is a snapshot; a recompute of an
older day keeps the row it has). Read from Firestore through the same reads as the Cabinet's `indexing_status`, plus
the day's model usage.

| Field | Type | Meaning |
|---|---|---|
| `day`, `user_hash`, `snapshot_version` | DATE, STRING, INT64 | |
| `enabled`, `building`, `awaiting_rebuild`, `access_lost` | BOOL | |
| `counters` | ARRAY<STRUCT<k STRING, n INT64>> | the status counters (state counts, described, not described, not describable, failed, refused, unread, cards) — key/value so a new counter needs no column |
| `readable_chunks` | INT64 | |
| `usd_day`, `usd_month`, `usd_caps` | ARRAY<STRUCT<k STRING, usd FLOAT64>> | per usage class |
| `fallback_usd_day` | FLOAT64 | dollars spent on the Gemini fallback |
| `lag_hist` | ARRAY<INT64> | settle-lag histogram of the day (§6.2 edges `lag_v1`) |
| `lag_edges` | STRING | edge-set id |

### 5.4 `drive_metrics` (permanent; partition `day`, cluster `metric_id`)

The aggregate. **Day rows only**; every period is a sum of day rows.

| Field | Type | Meaning |
|---|---|---|
| `day` | DATE | UTC day of the unit (search, turn, snapshot, judged search) |
| `metric_id`, `metric_version` | STRING, INT64 | §7 |
| `user_hash` | STRING | 16 hex, or `"*"` for all users |
| `dims` | ARRAY<STRUCT<k STRING, v STRING>> | `release`, `search_build`, and per metric e.g. `reason`, `judge_version` |
| `numerator`, `denominator` | FLOAT64 | a rate is `Σnum / Σden` over any set of rows |
| `n` | INT64 | units counted (searches, turns, judged items) |
| `sum_sq` | FLOAT64 | Σ x² of the per-unit value, for the interval of a mean (§6.3) |
| `histogram` | ARRAY<INT64> | counts per bucket of the metric's fixed edges |
| `edges` | STRING | edge-set id (part of the metric version) |
| `rollup_version`, `computed_at` | INT64, TIMESTAMP | |

### 5.5 `drive_judge_verdicts` (30 days) and `drive_metrics_reports` (permanent)

`drive_judge_verdicts`: `week, ts, trace_id, event_id, user_id, item_kind (sample | flagged | anchor | rejudge),
anchor_id, model, effort, judge_version, prompt_sha, verdict (JSON), note (≤ 300 chars), cost_usd, ok`.
`drive_metrics_reports`: `week, posted_at, builds_compared, message_sha` — the exactly-once marker of the weekly post
and a record of what was compared.

### 5.6 S0 tables (permanent)

`drive_turn_baseline_v0`: the counts-only turn backfill from 2026-10-10 (§12, S0): `day, trace_id, user_hash,
llm_calls, token sums, cost_usd, wall_s, search_calls, opens, lists, mutations, other_intents, parse_ok`. No text,
no raw id. `drive_search_baseline_v0`: the `[DriveSearch]` log lines of the same window parsed into their counts
(`ts, trace_id, user_hash, format v1|v2`, the line's numeric fields). The line's `user=` is an 8-character prefix of
the id; S0 maps it to the full id through the user list before hashing and drops it if ambiguous.

## 6. Aggregation model

### 6.1 Rows that re-aggregate

Each metric reduces one day's units to one row per `(metric_id, metric_version, user_hash, dims)`:
- **Rates and ratios** carry `numerator` and `denominator`. A week, a month, a release or all time is
  `Σnumerator / Σdenominator` over the rows in it — exact, not an average of averages.
- **Distributions** carry a histogram over **fixed edges** declared with the metric. Histograms add bucket-wise, so
  p50/p90 of any period come from the summed histogram. A quantile is reported as the upper edge of the bucket where
  the cumulative count reaches it (conservative, documented on the report).
- **Means** (searches per turn) carry `numerator = Σx`, `n`, `sum_sq = Σx²`.
- Every row is written twice: per `user_hash` and for `"*"`. The report reads `"*"` rows for totals and per-user rows
  for index health.

Day rows hold the `release` and `search_build` dims, so a release view is a sum over rows with that build, across
days. A day with two builds has two rows per metric.

### 6.2 Fixed edges (v1)

| Edge set | Edges (lower bounds; last bucket open) |
|---|---|
| `ms_v1` | 0, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000 |
| `chars_v1` | 0, 1000, 2000, 4000, 6000, 8000, 10000, 12000, 16000, 24000 |
| `tokens_v1` | 0, 25k, 50k, 75k, 100k, 150k, 200k, 300k, 400k, 600k, 1M |
| `usd_v1` | 0, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1 |
| `lag_v1` (seconds) | 0, 60, 300, 900, 1800, 3600, 7200, 14400, 43200, 86400, 259200 |

Changing an edge set is a new metric version (§6.5).

### 6.3 Intervals and the ▲/▼ rule

- Rates: Wilson score interval, z = 1.96 (the G3 bars already use it).
- Means: normal approximation from `n`, `Σx`, `Σx²`; shown only when n ≥ 30.
- Quantiles: the distribution-free binomial interval on order statistics, mapped to bucket edges.
- A change is marked ▲ or ▼ **only when the two intervals do not overlap**; otherwise "≈". A side with fewer than
  `MIN_N` units (30 searches, 15 turns, 20 judged items) shows "n too small" and no arrow.

### 6.4 What a "release" is

`K_REVISION` changes on every deploy, including a docs-only one, so pooling by revision would split the regression
view into slivers. Each event therefore carries both:
- `release`: the revision, for traceability;
- `search_build`: 12 hex of sha256 over the **source of the search path** — `drive_search_service.py`, the
  `domain/drive_*` modules it imports, `drive_index_params`, the `drive-search` system skill files — plus
  `KEYS_VERSION` and `POLICY_VERSION`. Computed once at startup in composition from the files in the image.

The report's "release" is a `search_build`: consecutive revisions with the same fingerprint pool into one. The turn
metrics also depend on Smart's prompt and model, which live in Firestore and user config; a change there does not
move the fingerprint. The report therefore lists, beside each build, the prompt snapshot date of the drive protocol
tokens when known, and treats a turn-metric change without a build change as "not attributed".

### 6.5 Versioned definitions

Each metric is a `MetricDef(id, version, unit, kind, better, edges, compute)` in `src/domain/drive_metrics.py`.
- A change of meaning or edges bumps `version`. The old and new versions are computed side by side for an **overlap
  window** of at least 14 days (the rollup recomputes the last 30 days of the new version from the detail tables),
  after which the old version may be retired. Older history stays in the old version: **a definition cannot be
  recomputed more than 30 days back** (§11).
- **Pin test:** every `(id, version)` has a golden output on a committed fixture of events and turns. A code change
  that alters a pinned output fails `make check`; the fix is a version bump with a new golden, never an edit of the old
  one (CLAUDE.md test rule).

## 7. Metric catalogue (v1)

Unit: **S** = search event (`mode = query` unless stated), **T** = drive turn, **I** = index snapshot,
**J** = judged search. "Noise" is the expected week-to-week spread at today's volume.

| id | Unit | Definition | Better | Noise / week | Detects |
|---|---|---|---|---|---|
| `search.exact_rate` | S | About or Mentioned total > 0 | ↑ | ±10 pp | keyer or normaliser regressions |
| `search.auto_only_rate` | S | only By translation or Labelled are non-empty | ↓ | ±6 pp | profiler or doc2query drift |
| `search.related_only_rate` | S | only Related is non-empty | ↓ | ±5 pp | `Z_RELATED` |
| `search.empty_rate` | S | `returned = 0` (all modes, `status = ok`) | ↓ | ±6 pp | blocking bugs |
| `search.d29_rate` | S | `absent > 0` | watch | ±5 pp | `COMMON_SHARE`, keys |
| `search.incomplete_rate` | S | coverage incomplete; dims `reason` (one row per reason, a search counts under each of its reasons) plus an `any` row | ↓ | low | a stalled enrichment, hidden behind "rebuild" |
| `search.chars` | S | histogram `chars_v1` → p50/p90 | ↓ | low | result bloat |
| `search.ms` | S | histogram `ms_v1` → p50/p90 | ↓ | low | Firestore fan-out |
| `search.status_rate` | S (all) | dims `status` | watch | — | gate failures |
| `turn.searches_per_q` | T | mean `search_calls` | ↓ | ±0.3 | the Barcelona pattern |
| `turn.tokens` | T | histogram `tokens_v1` → p50/p90 | ↓ | ±25 % | context bloat |
| `turn.cost` | T | histogram `usd_v1` → p50 | ↓ | ±25 % | cost per question |
| `turn.crawl_rate` | T | `listings_after_search ≥ 2` | ↓ | ±6 pp | distrust of the index |
| `turn.needless_open_rate` | T | Σ`opens_of_about_shown` / Σ`opens` | ↓ | ±10 pp | weak cards |
| `turn.after_d29` | T with D29 | dims `outcome` (`dropped_word`, `other_research`, `opened`, `listed`, `stopped`) | watch | high | the D29 line |
| `turn.redundant_lang_rate` | T | `redundant_lang_researches > 0` | ↓ | ±5 pp | the protocol's tiers line |
| `judge.top1` | J | among judged searches with a grade-2 file shown, the best file is first | ↑ | ±10 pp | ranking inside tiers |
| `judge.precision` | J | Σ shown files graded ≥ 1 / Σ shown files | ↑ | ±10 pp | junk in tiers |
| `judge.junk` | J | Σ shown files graded 0 / Σ shown files | ↓ | ±10 pp | |
| `judge.honest_nomatch` | J | among searches with no About/Mentioned file, no probe file is a better answer | ↑ | ±10 pp | false "No file holds" |
| `judge.missed_better` | J | a probe file is a better answer than every shown file | ↓ | ±10 pp | recall without labels |
| `judge.anchor_agreement` | J (anchors) | anchors whose key fields match the expected labels (§8.4) | ↑ | — | judge drift |
| `judge.self_agreement` | J (re-judged) | re-judged items with the same key fields | ↑ | — | judge noise |
| `index.described_share` | I | described / text files | ↑ | low | enrichment progress |
| `index.failed_share` | I | failed or refused / files | ↓ | low | reader and parser health |
| `index.usd_per_file` | I | Σ`usd_day` / files settled that day | ↓ | low | cost drift |
| `index.lag` | I | histogram `lag_v1` → p90 | ↓ | low | a stalled tick |
| `index.cap_use` | I | `usd_month / usd_caps` per class (gauge, last day of the period) | ↓ | low | a budget about to stop work |
| `data.parse_ok` | T | turns with `parse_ok` | ↑ | — | serialization drift (decision 3 trigger) |
| `data.event_gap` | T | Σ(`search_calls` − `search_events`) / Σ`search_calls` | ↓ | — | dropped events, refused calls |

All judge metrics carry the dim `judge_version` (§8.3), so a judge change starts a new series instead of reading as a
search regression.

## 8. The judge

### 8.1 Sample

Weekly, over the previous ISO week (UTC days), **only searches of users whose `drive_search_judging` flag is on**
(`UserBotConfig`, default false; the owner sets it for himself, §12 S5):
- **Flagged**, up to 20: `returned = 0`, a D29 line, coverage reason `full_page`, or a search in an outlier turn
  (top 5 % of the week by tokens or `search_calls ≥ 4`).
- **Random**, 30, from the rest.
- **Re-judge**: 10 % of the above (5) judged a second time in the same run.
- **Anchors**: 30 fixed cases (§8.4).

Sampling is seeded by the week key, so a retried run judges the same items. With fewer eligible searches the sample
is everything.

### 8.2 Input and output

**Input** (one user message, built by the runner; file ids replaced by aliases `f1…fn`):
- the `drive_query`, the filters and the `rendered_result` exactly as Smart read it;
- for each shown file: its **current** card text and profile (read from the index at judge time; a file deleted since
  is marked so and graded only from the rendered result);
- a **probe**: the top 20 card-kNN files for the query that the tiers did not show (pseudo-recall), with their cards
  and profiles.

**Output** — JSON under `OUTPUT_FORMAT_DRIVE_JUDGE` (Claude structured output; `json.loads`, no regex fallback,
retry once on invalid JSON — CLAUDE.md, Agent Output Format Standards):

```json
{"shown": [{"file": "f1", "grade": 2}], "best_is_first": true, "junk": 0,
 "nomatch_honest": null, "absent_word_in_probe": null, "probe_better": [], "note": "…"}
```

`grade`: 0 unrelated, 1 related, 2 answers the query. `best_is_first`: null when no file is graded 2.
`nomatch_honest`: only when no About/Mentioned file was shown. `absent_word_in_probe`: only with a D29 line — whether a
probe file does hold the "absent" word (a keyer miss). `note`: at most 300 characters, 30-day table only.

The rubric is the token `COGNITIVE_PROCESS_DRIVE_JUDGE`; the blueprint `drive_judge_agent_v1` and profile
`drive_judge` follow the drive agents' layout in `prompts_snapshot/`. No fallback prompt in code.

### 8.3 Stability

- **Pinned model and effort:** `JUDGE_MODEL = "claude-sonnet-5-5"`, `JUDGE_EFFORT = "medium"` in
  `domain/drive_judge.py`. The runner builds the agent with a pinned configuration (provider `claude`, model override,
  thinking), not the user's. The `drive_judge` provider strategy allows `claude` only and has **no fallback**: a
  verdict from another model is worse than no verdict. A judge outage skips the week's judge section; it is not
  retried into another provider.
- **Series key:** every verdict stores `judge_version` (code constant, bumped with any rubric or input change),
  the model id that ran, and `prompt_sha` (12 hex of the assembled system prompt). The metric dim `judge_version` is
  `"{JUDGE_VERSION}:{model}:{prompt_sha}"`: a prompt upload in Firestore starts a new series by itself, so a forgotten
  constant bump cannot splice two judges into one line.
- **Model change:** the old and new judge run side by side for one week on the same sample, then the old one stops.
- **Anchors** (§8.4) every week. A drop of `judge.anchor_agreement` below its recorded bar (the Wilson lower bound of
  the first two weeks) marks the judge section "judge drift — not compared".
- **Self-agreement** on the re-judged items measures noise; it is shown, not barred.

### 8.4 Anchors

30 cases from the synthetic drive eval fixture (`gold_v2.json`), stratified by family (exact, semantic, class,
misspelling, incident, negative), **pre-rendered offline** with the real search over the recorded enrichment
(`search_v2_eval` machinery, no model call) and committed as `src/assets/drive_judge/anchors_v1.json`: query,
filters, rendered result, shown cards and profiles, probe cards and profiles, and the **expected labels** from the
gold — which aliases are gold, whether a gold file is first, and for negatives `nomatch_honest = true` and an empty
`probe_better`. Agreement on an anchor means: every gold file shown is graded ≥ 1, `best_is_first` matches, and for a
negative, `nomatch_honest` and `probe_better` match. The fixture is PII-free, so anchors may go to any provider.
`anchors_v2` is a new file and a new `judge_version`; the old file is kept.

### 8.5 Cost

About 10k input and 600 output tokens per item. At Sonnet 5.5's $2 / $10 per million (`domain/billing.py`) that is
≈ $0.026 per item. A week is ≈ 85 items (50 sampled + 5 re-judged + 30 anchors) ≈ **$2.2 / week, ≈ $9.5 / month**.
Calls are billed through the agent's normal `TokenLedger` path to the account of the user whose search is judged
(anchors: the first opted-in user's account), and each verdict row carries its `cost_usd`.

## 9. Privacy and retention

| Data | Where | Retention | Identity | Text |
|---|---|---|---|---|
| Search events | `drive_search_events` | 30 days (partition expiry) | raw `user_id` | query, words, rendered result (may hold document passages) |
| Turn facts | `drive_turn_facts` | 30 days | raw `user_id` | none |
| Judge verdicts | `drive_judge_verdicts` | 30 days | raw `user_id` | `note` ≤ 300 chars |
| Index snapshots, metrics, report markers, S0 baselines | permanent tables | permanent | `user_hash` only | none; only enum dims |
| Weekly report | dedicated Slack channel | Slack's | `user_hash` | **none**: counts, hashes, trace links — no query text, no file names |

- **The HMAC key** is a Secret Manager secret exposed as `DRIVE_METRICS_HASH_KEY` and registered in
  `load_settings()` (it is a secret). Without it the rollup **fails closed**: it writes no permanent row and logs ERROR.
  The key is never rotated except after a leak; rotation breaks per-user continuity, and the report says so.
- **Erasure:** a user's permanent rows are found by recomputing their hash with the key and deleted by a runbook query;
  the `"*"` rows keep the totals. Turning drive indexing off does not delete metrics: they describe searches already
  made, as `prompt_content` does.
- **The judge** sends one user's query, rendered result and file profiles to Anthropic for a purpose the indexing
  disclosure does not name. Hence decision 4: the owner's own searches only, behind an explicit per-user flag.
  Before any other user's flag is set, the Cabinet disclosure gains a line and the user opts in (open question 2).
- **The dataset** is separate from `prompt_content`'s, with the same access (the owner and the runtime service
  account). Detail tables expire by partition; nothing copies their text elsewhere.
- **The Slack report** is the only artefact outside GCP; it carries no text by construction (the renderer takes typed
  numbers, not strings from events).

## 10. The weekly report

Monday 08:00 Europe/Madrid, for the previous ISO week. One Slack message (mrkdwn), at most ~45 lines:
1. **Header:** the week, searches / drive turns / judged items, and the search builds seen with their first and last
   day.
2. **Build vs previous build** (primary): each KPI with both values, both intervals and ▲/▼/≈ (§6.3).
3. **Weekly trend:** this week, the previous week and the 4-week baseline, same rule.
4. **Judge** (S5): the judge metrics, anchor agreement against its bar, self-agreement, the series key and the cost.
5. **Index health** per `user_hash`: described and failed shares, settle-lag p90, cap use, fallback dollars.
6. **Outliers**, at most 10: reason ("8 calls / 371k tok / 9 listings"), a Logfire trace link (base URL from
   `LOGFIRE_TRACE_URL_BASE`, read with `os.getenv`) and a `bq` snippet that selects the trace from the event table.
7. **Data health:** `data.event_gap`, `data.parse_ok`. Below 99 % `parse_ok` in two consecutive weeks the line reads
   "re-decide the turn source (RFC decision 3)".

The post is exactly-once per week: the task's Cloud Task name is the week key (a scheduler retry is a duplicate), and
`drive_metrics_reports` is checked before posting and written after.

## 11. Risks

1. **Small n.** Weekly deltas under ~10 pp are noise. Mitigation: build-vs-build pooling, intervals, "n too small".
2. **Turn facts depend on how `tool_calls` are serialized.** A change in `BigQueryPromptContentAdapter._build_record`
   silently breaks the parse. Mitigation: `parse_ok` on every row, a fixture test that runs the writer's real
   `_build_record` output through the parser, and decision 3's re-decide trigger.
3. **A new metric version cannot be recomputed more than 30 days back.** Mitigation: overlap windows (§6.5); write the
   definition before the history is needed.
4. **Best-effort writes can drop events**, and Cloud Run throttles CPU once a response is sent. The search runs
   mid-turn, so the write normally completes while Smart is still working. Mitigation: `data.event_gap` in every report.
5. **The judge sends profiles to a provider for a new purpose.** Mitigation: owner only, flag-gated (§9).
6. **BigQuery DML cannot touch rows still in the streaming buffer.** The idempotent rewrites therefore use **load
   jobs into a partition decorator with `WRITE_TRUNCATE`** (atomic per day, free), never streaming + DELETE. Only the
   hot-path event table streams, and it is never rewritten.
7. **Judge drift reads as a regression.** Mitigation: series key, anchors, a marked "judge drift" state.
8. **Failed LLM calls have no `prompt_content` row**, so a turn that failed half-way is under-counted. Accepted;
   Logfire holds the failure.
9. **Fingerprint blind spots:** a Smart prompt or model change moves turn metrics without moving `search_build`
   (§6.4). Mitigation: "not attributed" on the report.

## 12. Rollout

| Slice | Content | Effort | Depends on |
|---|---|---|---|
| **S0 — by 2026-11-09 00:00 UTC (target: week of 2026-10-26)** | HMAC key secret (owner); `scripts/metrics/drive_baseline_s0.py` (dry run by default, `--apply` by the owner): the counts-only turn backfill from 2026-10-10 into `drive_turn_baseline_v0`, and the `[DriveSearch]` log lines of the same window into `drive_search_baseline_v0`. Ships the `tool_calls` parser and the log-line parser with their tests; S2 reuses the first. | 0.5 d | — |
| S1 | `DriveSearchEvent`, `DriveTelemetrySink`, BigQuery and in-memory adapters, emission in `DriveSearchService`, `search_build` fingerprint, dataset/env wiring | 2–3 d | — |
| S2 | Row model, metric registry with pin tests, turn-fact derivation, `DriveMetricsStore`, `DriveMetricsService.rollup_day`, `drive_metrics_rollup` task and scheduler job, backfill script for the 30-day window | 2–3 d | S1 |
| S3 | Weekly report (without judge and index sections), fan-out + named Cloud Task, dedicated webhook, outliers, report marker | 1–2 d | S2 |
| S4 | Index snapshot, settle-lag histogram on the indexer, `index.*` metrics, report section | 1–2 d | S2 |
| S5 | Judge agent, runner, port, prompts (owner upload), anchors asset and its builder, sampling, `judge.*` metrics, report section, owner's opt-in flag | 2–3 d | S3 |
| S6 (option, after 4 weeks of S5) | 👍/👎: Slack `reaction_added`, a `ts → trace_id` map (30-day TTL), a label table, judge-vs-label agreement | 2 d | S5 |

Each slice is its own PR from a feature branch; docs (this RFC's status, the building block, CLAUDE.md's
`WorkerHandler` list, `docs/07_deployment/SCHEDULERS.md`) move with the code.

**Acceptance per slice:** S0 — row counts per day match a counts query on `prompt_content`; the owner spot-checks the
Barcelona turn. S1 — one live search yields one event whose `trace_id` joins its `prompt_content` rows. S2 — a
rollup of a live day reproduces the S0 counts for that day within the `parse_ok` share; re-running it leaves the same
rows. S3 — a manual weekly run posts once; a second run posts nothing. S4 — a snapshot row per user; lag histogram
non-empty after a tick that settled records. S5 — anchor agreement recorded for two weeks; the cost line within 30 %
of §8.5.

## 13. Open questions

1. **Retire the `[DriveSearch]` log line** once events are proven (8 weeks of `data.event_gap` ≈ 0)? The line is
   the only signal when BigQuery is down; keeping it costs nothing. *Lean: keep.*
2. **Judge for other users:** the opt-in surface (a Cabinet toggle with a disclosure line in four languages) is
   designed only when a second user is asked.
3. **Prompt attribution for turn metrics:** could the prompt-snapshot workflow stamp a content hash of the drive
   protocol tokens that Smart's rows could carry? Would move "not attributed" to attributed; not needed for v1.
4. **Looker Studio** on `drive_metrics`: nothing to build here; decide when the weekly message stops being enough.
5. **S6 labels** may show the judge agrees poorly with the owner; then the rubric, not the search, is revised first.

## 14. Alternatives rejected

- **Logfire metrics** — free up to 10M records/month, but retention is 30 days (90 on a $249/month plan): fails
  "all periods".
- **Firestore events + aggregate docs** — no SQL, no percentiles, no join to `prompt_content`.
- **BigQuery scheduled queries for the rollup** — SQL outside git, no tests, hand-edited.
- **A Cloud Run Job for the rollup** — a fourth deploy unit for work that takes seconds.
- **GCS Parquet/JSONL archive of events** — only a long-term text archive would need it, and that is excluded.
- **A Smart-side turn event now** — precise, but no history, more hot-path code; kept as the fallback of decision 3.
- **Pooling by revision** — every docs deploy would split the regression view (§6.4).
