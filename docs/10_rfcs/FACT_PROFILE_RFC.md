# RFC: Fact profile — a per-fact retrieval profile, one domain registry and multi-list memory search

**Status:** Accepted by the owner 2026-11-09 (§12). Implementation in progress (§9 PR series).
**Owner:** Dmytro Deleur.
**Design inputs:** the design inputs (taxonomy, indexing rules, profiler prompt drafts, code survey, offline prototype)
are private working documents; measured numbers quoted here are aggregates from those runs.

**Settled before this RFC (not reopened here):** one Sonnet 5.5 call per fact (effort high, adaptive thinking, cached
system prompt, JSON-schema output); code computes domains and validates everything the model proposes; a profile
carries the fingerprint of its input and every consolidation cycle profiles the facts whose profile is missing or stale;
seven search lists fused by RRF with the domain list kept whole, collect-all mode, plain domain intersection, time role
in ordering, recurring occasions by month and day, the intimate gate; query slots from the frozen P1 prompt on the
cheap model; one domain registry; a one-time backfill; automatic people cards at ≥ 10 facts; no
migration or rollback insurance; fact text is never rewritten.

## 1. Context and goal

A fact today carries its text, three embeddings (`vector`, `tags_vector`, `metadata_vector`), free tags, metadata and
one of 16 domains chosen by the consolidator (`src/domain/entities.py:18`, `:111`). Search fuses up to seven vector and
domain channels (`search_enrichment_service.py`, memory map §4.3). Three things fail in measured runs:

- **Words.** People ask about their lives in situation words ("the trip", "the birthday"); facts are written in record
  words (a booking, a date). The tag and metadata vectors repeat the text vector (cosine ≈ 0.86–0.88); in the
  "before" run their four channels together found three targets the other channels missed.
- **Time, places, people.** A fact's date, city and people are inside its text only. 41 % of facts located in a country
  never name it; the offline atlas resolves 92 % of city mentions. Asking by relation fails when facts name the person.
- **Domains.** The 16-value domain mixes area, kind and privacy, and is chosen from definitions by the consolidator.

**Goal.** Every current fact gets a **profile**: label words that bridge record words to life words, a time role and
printed time points, atlas-resolved places, people mentions, the 27-domain placement computed by code, kinds and
sensitivity. Search becomes one engine with seven lists over those fields, filled by one query contract, used by the
Router prefetch, `search_memory`, the claude.ai connector and Lelik. Every reader of today's `FactDomain` moves to the
new fields through one registry.

**Measured basis (offline prototype over the rebuilt store, control questions, 2-of-3 runs of the cheap slot model).**
Frozen variant after the tuning round: P1 slot prompt + kinds list + recurring occasions.
Design split: Router path required 24/24, set 37/47 (before 22/24, 33/47); `search_memory` path required 17/17, set
25/26 (before 17/17, 24/26); connector path required 20/20 (before 12/20). Held-out split, run once: Router required
7/7, set 23/29 (before 7/7, 22/29); `search_memory` 7/7 (before 6/7); connector required 4/5 (before 5/5 on two
questions — the one miss is a fact that no list reaches; §12, item 9). Per list, leave-one-out unique targets: semantic 12, labels 6–8, places 5–6, time 2–3, people 1, domain
0–1; the domain list still carries area-wide recall (a collect-all question over one area reaches 1.00 of its set with it,
0.62 without). The slot call costs ≈ $0.001 and takes ≈ 1.1 s at p50.

## 2. Data model

### 2.1 Profile fields on `FactEntity`

Flat, top-level, optional fields on the fact document (`{prefix}domain_facts_v2`). Written **only** by the profiler
step through `write_fact_profile` (§2.3); never by the consolidator, the review or the Cabinet.

| Field | Type | Written from | Read by |
|---|---|---|---|
| `label_words` | list of 0–12 strings, 1–3 lowercase English words | model, checked by code | labels list (via vector), intimate rule |
| `label_vector` | 768-dim vector of the joined labels (`RETRIEVAL_DOCUMENT`) | code | labels list |
| `time_role` | `event` / `state` / `validity` / `measurement` / `timeless` | model | list ordering |
| `time_points` | ≤ 8 maps `{from, to, precision, source}` | model, each point verified as printed | time list, ordering, `when:` line |
| `place_keys` | `pl:<geonameid>` (with the large neighbour) and `co:<country>` | code from the model's `{city, country, source}` via the atlas | places list |
| `places_unresolved` | list of `{city, country}` with no single atlas row | code | reports only |
| `people` | list of `{name, role, name_keys, role_keys}` | model names and roles; keys by code (`skeleton_key`, word forms) | people list, people cards |
| `primary_domain` | registry key | code (§3.3) | display, voice filter, baseline grouping |
| `domain_keys` | registry keys: primary, hard extras, ≤ 2 topical extras | code | domain list, Cabinet filter |
| `user_identity` | bool | model answer, stored | baseline identity block |
| `kinds` | subset of the seven kinds | model answer | kinds list |
| `sensitivity` | `ordinary` / `discreet` / `intimate` | model answer | intimate gate, voice baseline |
| `profile_answer` | map: the model's validated raw answer | code | re-derivation without a call |
| `profile_fingerprint` | sha256 of the profiler's user message | code | staleness |
| `profile_derivation` | code-derivation version string (§4.2) | code | re-derivation trigger |
| `profile_drops` | map reason → count | code | observability |
| `profile_model`, `profiled_at` | string, timestamp | code | observability |
| `profile_failures`, `profile_failed_fingerprint` | int, string | code | abandon rule (§4.4) |

**Worked example (invented universe).** Fact: *"Alex's car insurance with Example Mutual Insurance, policy
MP-2026-118204, runs 01/04/2026–31/03/2027 and costs 412.00 EUR."* A correct profile has time role `validity`, one point
`{from: 2026-04-01, to: 2027-03-31, precision: day, source: "01/04/2026–31/03/2027"}`, no places or people, subject
`transport` with money flow, so `primary_domain = transport`, `domain_keys = [transport, money]`, kind `document`,
sensitivity `ordinary`, and labels that are not in the text (the word "insurance" would be dropped as a repeat).

### 2.2 What does not change

`text`, `metadata`, `vector`, `tags`, `tags_vector`, `metadata_vector`, `type`, `temporal_class`, the lifecycle and
expiry fields and the old `domain` field keep their writers and values. The profiler never writes them (a test pins
`PROFILE_FIELDS` disjoint from them and from `IN_PLACE_FIELDS`, `fact_lifecycle.py:48`). The old `domain` keeps being
written by the consolidator until PR 13 (§9) and is read only as the directive marker after PR 12.

### 2.3 Writing a profile

`FactRepository.write_fact_profile(fact_id, fields, expected_fingerprint)` — a transaction that refuses any key outside
`PROFILE_FIELDS` before I/O (as `require_in_place` does) and writes only when the document's stored
`profile_fingerprint` still equals the value read at selection (compare-and-set). A document's text and metadata never
change in place — a content change is a new document (`fact_update_plan.py:40-68`) — so the CAS guards against another
profile write, not against a text change. A lost CAS is counted and the fact is picked up by the next cycle.

**A content version copies the old profile.** `plan_fact_update` builds the new version with `model_copy`, so it carries
the previous version's profile and fingerprint; its input hash differs, so it is stale and is re-profiled in the same
cycle (or the next, for a write outside a cycle). Until then search reads the old profile. Clearing it instead would
drop the fact from the identity block, the domain and kinds lists — worse than a briefly stale label set. A tags-only
version keeps text and metadata, so its profile stays fresh (code survey surprise 4).

### 2.4 Indexes (deploy and wait for READY before the code that queries them)

| Index on `{prefix}domain_facts_v2` | Serves | Needed before |
|---|---|---|
| `account_id` ASC, `state` ASC, `label_vector` VECTOR(768, flat) | labels list `find_nearest` | PR 8 (first search caller) |
| `account_id` ASC, `state` ASC, `user_identity` ASC, `context_priority_rank` ASC, `created_at` DESC | baseline identity block | PR 11 |
| `account_id` ASC, `state` ASC, `domain_keys` CONTAINS, `created_at` DESC, `__name__` DESC | Cabinet domain filter | PR 12 |

The profile scan and the search snapshot (§5.2) read `account_id == X AND state == current` with a projection; the
existing `(account_id, state, __name__)` index serves them. The old `domain` indexes stay until PR 13.

## 3. Domain registry

### 3.1 Shape and place

`src/domain/fact_domains.py` — pure data, stdlib only, the single source of truth (mirrors `agent_manifest.py`: typed key
constants plus one tuple of declarations). It lives in `domain/` because the pure derivation (§3.3), the prompt
renderers and the domain-layer search code read it; `agent_manifest.py` sits in `infrastructure/` only because its
descriptors do.

```python
class Domain:                      # typed constants; stored values, never renamed (a rename changes labels only)
    BIOGRAPHY = "biography"; FAMILY = "family"; ...; AGENT_DIRECTIVES = "agent_directives"

@dataclass(frozen=True)
class FactDomainEntry:
    key: str                        # Domain.* value, stored in primary_domain / domain_keys
    labels: Mapping[str, str]       # UI label per locale: en, uk, fr, es
    query_row: Optional[str]        # the LIFE AREAS row of the query prompt; None for agent_directives
    subject_row: Optional[str]      # the SUBJECT ENUM row of the profiler; None for flag-decided domains
    use_area: bool                  # member of the USE-AREA enum
    voice_open: bool                # in Lelik's default list
    cabinet_order: int
    legacy_config_values: tuple[str, ...]   # old FactDomain values this key replaces in STORED CONFIGURATION only

FACT_DOMAINS: tuple[FactDomainEntry, ...]   # 27 entries, in the taxonomy's order
```

Flag-decided domains (`biography`, `family`, `people`, `pets`, `state_law`) have no subject row; `agent_directives` has
neither a subject nor a query row. The subject enum (21 rows + `none`) and the use-area enum (21 + family, people, pets,
state_law) are derived from the entries, never listed elsewhere.

### 3.2 Consumers

| Consumer | Reads |
|---|---|
| Profiler prompt | `subject_row`s rendered as the closing `SUBJECT ENUM` / `USE-AREA ENUM` block (§4.3) |
| Profiler output schema | subject and use-area enums |
| Query prompt P1 | `query_row`s rendered as the closing `LIFE AREAS` block; the `domains` enum of the slot schema |
| Search | key validation of `domains` slots and MCP parameters |
| Lelik | `voice_open` default |
| Cabinet | `GET /api/user/fact-domains` (key, label in the page language, order), replacing the page-script map (`cabinet.html`) |
| Baseline formatter, connector output | section labels |
| Stored configuration | `legacy_config_values` mapping for `voice_fact_domains` and companion `session_domains` |

**Architecture test** (`tests/unit/test_arch_single_domain_registry.py`): an AST scan fails when any module outside
`fact_domains.py` holds a list, tuple, set or enum with three or more registry keys as string literals; a second test
fails when a locale lacks a label for a key. Adding a domain = one entry + deploy; no token upload.

### 3.3 Derivation (`src/domain/fact_profile_derive.py`)

Code applies the taxonomy's precedence rule verbatim: directive → tax/fine → identity → pet → state/law → person → subject; hard extras
(money flow, the tax/fine pair) uncapped; topical extras ≤ 2 in the order subject-when-a-flag-decided, other flag hits,
use areas; `none` without a flag → `lifestyle` plus a counted profiling error. The directive input is read from the fact
(`is_directive`, §6 row 15), never asked of the model; a directive gets `primary = agent_directives`, no extras, and **no
model call** (labels and time are not used for directives).

## 4. Profiler

### 4.1 Model contract

`FactProfilerAgent` (`src/agents/fact_profiler_agent.py`), internal, built by its runner in composition, never delegated. Model pinned
by the optional knob `FACT_PROFILER_MODEL` read with `os.getenv` at the call site (default: the Sonnet 5.5 id); effort
high, adaptive thinking (the stall fix of `decisions/sonnet55_adaptive_thinking_stall.md` applies), system prompt with
`cache_control`, output by JSON schema (`CLAUDE_STRUCTURED_OUTPUTS_GA_RFC.md`), `allow_provider_storage=False`, one parse
retry. User message: `Fact:\n<text>`, then `\nMetadata:\n` and one `key: value` line per key in sorted key order (lists
and objects as compact JSON) — the exact bytes the fingerprint hashes. No `Today:`, no reported date: the answer depends
on the fact alone, so a fingerprint holds for the fact's life.

### 4.2 Fingerprint and staleness (decided)

- `profile_fingerprint = sha256(user message)` — text and metadata only. Excluded on purpose: tags, `reported_date`,
  expiry, state, priority, the old domain. None of them is model input, and a tags-only or lifecycle-only change must
  not cost a call.
- `profile_derivation = DERIVE_VERSION | registry digest | atlas digest | is_directive`. A mismatch means only code
  inputs changed: the step re-derives from `profile_answer` without a call.
- `classify_profile(fact)` (pure) → `missing` (no fingerprint) / `stale_input` (fingerprint ≠ hash of the current
  message) / `stale_derivation` / `abandoned` (§4.4) / `fresh`.
- **Scope:** every `state == current` fact of the account being processed, including the history-trail fact a history
  rewrite creates (W5, `fact_review_answers.py:133`): it is current, archival and searchable, so it is profiled like any
  other. **SYSTEM-account facts are out**: the step runs per user account inside that account's cycle, and the SYSTEM
  account never consolidates; its facts are prompt components read by lineage (`prompt_builder.py:62`).

### 4.3 The prompt token

Two Firestore tokens, uploaded before the agent ships: `COGNITIVE_PROCESS_FACT_PROFILER` (the v4 general rules, label
words, time role, time points, places, people, then the domain questions — the decided "hybrid": each question's why-paragraph,
then its crisp rules) and `OUTPUT_FORMAT_FACT_PROFILER`. The closing `SUBJECT ENUM` / `USE-AREA ENUM` block is a
code-rendered static block from the registry, appended after the tokens; it ships by deploy. Text explains, then states
rules, and carries no examples. No inline fallback: a missing token fails the step (`[FactProfile] token missing`),
nothing is written.

### 4.4 Placement, budget, failure, cost

- **Where:** a `FactProfileService.run(account_id, deadline)` step at the end of `ConsolidationService._consolidate_leased`
  (after the agent, whatever its outcome) and at the end of `FactReviewService._daily`, both inside the account's
  memory-write lease (`consolidation_service.py:239`, `fact_review_service.py:311`). The review run covers writes made
  outside any lease — the Cabinet answer path (W11, `fact_answer_service.py:136-140`) — within a day, and every future
  writer without a hook of its own.
- **Order inside the step:** one projected scan of the account's current facts → classify → free work first
  (re-derivations, directives) → model calls for `missing`/`stale_input`, newest `created_at` first, at most
  `FACT_PROFILER_MAX_PER_RUN` (knob, default 20; 0 turns the step into re-derivation only), four at a time, stopping
  when the lease deadline minus its margin is near → embed the surviving label words in one batch → CAS writes →
  rebuild people cards (§7) → one baseline refresh after any written profile (the search snapshot is keyed by the
  baseline's `refreshed_at`, so new labels, times, places and people stay invisible to search without it).
- **Failures:** a failed call, an invalid answer after the retry, or a profiling error that requires a redo (an
  `intimate` answer with no surviving label) writes nothing but `profile_failures` and `profile_failed_fingerprint`;
  after three failures on one fingerprint the fact is `abandoned` until its content changes (logged once). A provider
  outage ends the step early; the rest wait for the next cycle. The step never fails the consolidation batch.
- **Validation and drops** (the code checks of the profiler design): labels — lowercased, ≤ 3 words, no digits, no
  lone record word (a one-word label naming a record, not a life situation), no
  relation word, no word form already in the text or metadata, deduplicated, cap 12; time points — the source occurs in
  the text or a metadata value (`drive_grade.TextEvidence`), the date is read from that source, `from ≤ to`, year
  1900–2100; places — printed source, city found in the named country by the atlas (`place_lookup.py`, the table loaded
  by `geo_place_table.py`), one row → keys, else `places_unresolved`; people — printed name or role. Every drop is
  counted per reason in `profile_drops`.
- **Cost:** ≈ $0.003 per fact (measured); steady state is the new and changed facts
  of the month, cents. Spend goes through the execution-scoped billing ledger as agent `fact_profiler`.
- **Observability:** per fact `[FactProfile] fact=<id8> outcome=written|rederived|failed|abandoned drops=… ms=… usd=…`;
  per run `[FactProfile] run scanned=… missing=… stale=… rederived=… written=… failed=… deferred=… usd=…`;
  `prompt_content` rows and Logfire spans come from the agent's normal call path.

## 5. Search

### 5.1 Query contract and slots

`FactQuery` (`src/domain/fact_query.py`): `subject`, `kind`, `people[]`, `places[{name, country}]`,
`time{intent, from, to}`, `domains` (≤ 2 registry keys), `kinds[]`, `scope` (`top`/`all`), `today`, `path`.
`parse_slots` validates in code: dates parsed and the window clamped (§5.3), unknown domains and kinds dropped, a slot
with Cyrillic refused and logged (the contract is English), `domain_link` read and ignored (§12 item 3).

Slots come from the **frozen P1 prompt** as a Firestore token (`OUTPUT_FORMAT_FACT_QUERY_SLOTS` with its rules; LIFE
AREAS rendered from the registry; `Today: <date> (<weekday>), timezone <owner zone>` appended per call), run by
`FactQuerySlotsAgent` on the tier of its caller — the Router's and `facts_memory`'s ECO tier (flash-lite). Inputs as
measured: the Router path passes the turn's messages; `search_memory` passes `SEARCH_REQUEST "<Smart's request>"`; the
connector and voice pass the caller's words (OD-2). A failed slots call degrades to one semantic list over the raw
request (no prompt involved), as `facts_memory` does today.

### 5.2 Where the lists read from

`FactSearchService` (implements `FactSearchPort`, `src/services/fact_search_service.py`) reads two kinds of input:

- the two kNN lists through `find_nearest` (`vector`, `label_vector`), depth 40, as today's channels do;
- the five key lists over an **account snapshot**: one projected read of the account's current facts (profile fields,
  text, `metadata`, `context`, `reported_date`, `created_at`; no vectors in the read), cached per instance and keyed by the baseline cache document's
  `refreshed_at`. Every fact writer already refreshes the baseline (memory map §4.1 "When it changes"), so the key moves
  whenever the facts do. The key lists, ordering, gate and fusion then run in pure domain code
  (`src/domain/fact_search_lists.py`), exactly as the validated prototype did. Text vectors are cached per account by fact id
  (a document's `vector` never changes, so a reload fetches only the ids it has not seen) and serve in-list similarity
  and deduplication. (OD-1 is the alternative: Firestore array queries per list.)

### 5.3 The lists

| List | Runs when | Candidates | Order inside the list |
|---|---|---|---|
| semantic | `subject` | `vector` kNN, 40 | similarity |
| labels | `kind` | `label_vector` kNN, 40; facts without labels excluded | similarity |
| domain | `domains` | every fact whose `domain_keys` hold the domain; two domains → facts carrying both, the union when none does. A full RRF list with equal weight — never a tie-break or a half weight (measured: no gain; the tie-break lowered a collect-all question) | in-list order, cut at 50 (`scope=all`: 100) |
| time | intent `future`/`past`/`period` | a point inside the window; a recurring occasion matches by month and day in any year | in-list order |
| time (latest) | intent `latest` | union of the other lists' candidates with a point | newest point first |
| places | `places` | `place_keys` ∩ the atlas query keys (a country reaches its cities; a city reaches its large neighbour) | in-list order |
| people | `people` | a relation word → facts whose mention role has it + every spelling of the matching cards (§7); a name → facts whose name keys match, widened by its card's spellings; with domains given, kept to those in the domains when that leaves any | in-list order |
| kinds | `kinds` | stored `kinds` ∩ requested | in-list order |

**In-list order** (key lists): with a window, distance to the window (0 on overlap, else days; MIN over a fact's
points; recurring facts projected into each year the window touches); intent `latest`, newest point first; otherwise by
time role, interleaved — state and validity newest first, event and measurement nearest to today (an undated fact uses
`reported_date`), timeless by subject similarity. **Windows** by code: `future` → `from` when it is after today, else today, to `to` or 365 days after that start;
`past` → `from` (or 365 days before the end) to `to` capped at today; `period` → both ends, one end ± 31 days;
`none`/`latest` → no window.

**Recurring occasion** (code rule, OD-7): a fact with a label phrase containing `birthday` or `anniversary`; each of its
points spanning at most 31 days (day or month precision) is projected into every year the window touches and its
neighbours (`fact_search_lists.py`). Example: Clara Moreira's birth date (14 July 1982) has a point
1982-07-14; asked in November 2026 "what is coming up next summer", its July projection falls inside the window.

**Intimate gate:** an `intimate` fact survives only in the labels list or the domain list; exception — `scope=all`
with people named (collect everything about a person). **Fusion:** RRF `1/(60 + rank)`, equal weights, existing
`src/domain/rrf.py`; then `SmartDeduplication(0.96, 0.98)`; cut 30; `scope=all` keeps every fused fact after dedup.
The Router path drops facts already in the baseline. One log line per search:
`[FactSearch] path=… lists=semantic:40,labels:40,domain:…,… fused=… kept=… gate_dropped=… unsnapped=… snapshot=hit|load:<n> ms=…`.

### 5.4 Callers

| Caller | Slots | Receives |
|---|---|---|
| Router prefetch | P1 call started in parallel with triage on the same messages; used when `search_intent ≠ none` (OD-3) | ≤ 30 minus the baseline, as `query_specific_context` |
| `search_memory` (`FactsMemoryAgent`) | P1 replaces the `MEMORYSEARCH_*` decomposer | 30 (`scope=all`: all), rendered with `context:`, `reported:`, `metadata:` and a new `when: <points at their precision>` line |
| claude.ai connector `get_user_context` | OD-2 | 30 / all, grouped by the registry label of `primary_domain`, with `when:` |
| Lelik via the voice token | as the connector | as the connector, then `VOICE_RESULT_MAX_CHARS` (8,000, `voice_mcp_token.py:26`) |
| Companion biographical slice | `subject` from its first phrase, `domains` from `session_domains` | as today |

`enrich_context` stays for the consolidator's `search_existing_facts` and the review's neighbour search (out of scope).

**Advice questions (proposal, OD-4).** Measured gap: "what should I do" questions do not pull the user's preferences and
constraints. Proposed rule in code: a query marked as asking for advice always adds the kinds list for `preference` and
`constraint`.

## 6. Reader migration

Every reader of today's `FactDomain` (code survey + taxonomy), its new rule and its PR.

| # | Reader | Today | New rule | PR |
|---|---|---|---|---|
| 1 | `biographical_context_service.py:146-170` | `domain == biographical` first, fill from all | identity block = `user_identity == true` (priority rank, newest); fill from all minus directives as today; cached items gain `primary_domain`, `sensitivity` | 11 |
| 2 | same `:185-202` | principles = tag `mindset` | unchanged | — |
| 3 | `biographical_formatter.py:37-169` | sections by domain; `[MINDSET]` only for domain `preference` | sections by registry label of `primary_domain`, biography first; `[MINDSET]` keys on the tag alone | 11 |
| 4 | `lelik_persona_service.py:60-64`, `user.py:66,309` | `domain in voice_fact_domains` (default biographical, preference) | `primary_domain` in the voice list (registry `voice_open` default: biography, food, lifestyle, abilities, beliefs, media, hobbies, sport, going_out, memories, appearance, belongings) **and** `sensitivity == ordinary`; a stored list equal to the old default reads as the default, any other through `legacy_config_values`, unknown values dropped with the existing warning | 11 |
| 5 | `prompt_builder.py:183-196` | directive split by `domain == agent_directive` | `is_directive(fact)` (row 15) | 12 |
| 6 | `companion_context_assembler_service.py:91-93` | directives by domain | `is_directive` query | 12 |
| 7 | `companion_context_assembler_service.py:136-180` | `enrich_context(…, relevant_domains)` | `FactSearchPort.search`; `session_domains` as registry keys | 12 |
| 8 | `firestore_channel_binding_adapter.py:72` | `FactDomain(d)` — raises on an unknown value | registry `config_key(d)`: legacy mapping, unknown dropped with a warning, never raises | 12 |
| 9 | `user_cabinet_app.py:842-1010`, `cabinet.html` | badge `f.domain`; `?domain=` filter (`firestore_repo.py:312`) | badge = registry label of `primary_domain` (old `domain` while absent); filter on `domain_keys`; labels from `/api/user/fact-domains` | 12 |
| 10 | `mcp_setup.py:175-182` | groups by `f.domain` | registry label of `primary_domain`; gate applied by the engine | 10 |
| 11 | `search_enrichment_service.py:135,363`, `firestore_repo.py:909-953` | domain channel `domain IN`, newest first | domain list of §5.3; old method kept for `enrich_context` users | 8–10 |
| 12 | `router_agent.py:310-338` | prefetch from triage `semantic_lens`/`search_phrase`/`relevant_domains` | P1 slots + `FactSearchService`; the triage fields stay emitted, unread, until PR 13 | 9 |
| 13 | `memory_search_agent.py:108-153, 241-247, 295-352` | `MEMORYSEARCH_*` decomposer, old domains, fallback vector search | P1 slots; fallback = one semantic list | 8 |
| 14 | `consolidation_agent.py:105-116` (+ tokens) | model supplies one of 16 domains | unchanged until PR 13, then the directive decision only (OD-6) | 13 |
| 15 | `fact_lifecycle.py:332`, `consolidation_agent.py:1664,1706` | directive = `domain == AGENT_DIRECTIVE` or principle or `mindset` | one predicate `is_directive(fact)` in `fact_lifecycle.py`, reading the old domain until PR 13 and the consolidator's flag after | 12 |
| 16 | `domain/search.py:114-129`, `consolidation_pass_stats.py:135` | consolidator sees and compares the old domain | unchanged until PR 13; then shows `primary_domain`, compares the directive flag | 13 |
| 17 | `fact_review_answers.py:149` | history fact copies `old.domain` | unchanged (legacy field); its profile comes from the step | — |
| 18 | `fact_management_adapter.py:172` | refuses a create without a domain | unchanged until PR 13 | 13 |
| 19 | `firestore_repo.py:1028` `get_legacy_facts` | "domain missing = legacy" (scripts only) | unchanged; it still means the old field | — |
| 20 | `firestore_repo.py:253`, `:359-440`, `:702-740`, `fact_review_service.py:591` | tags filter, three vector fields, write dedup, review neighbours | unchanged | — |

## 7. People cards

A card is a retrieval index entry: never a fact, never in a prompt, never read by the consolidator (so it does not touch
"the bot never writes memory"). Collection `{prefix}person_cards`, doc id `{account_id}:{card_key}`.

- **Built by code** at the end of every profile step (pure `build_person_cards(profiles)` in
  `src/domain/person_cards.py`, no model call): mentions are grouped by transliteration skeleton across scripts; a group
  with **≥ 10 current facts** becomes a card with its spellings, its fact count and its relation — the relation word
  (closed list in code: partner, wife, husband, son, daughter, mother, father, brother, sister and their kin) that most of
  its mentions carry as their role, if any. Example: Maria Doe written as "Maria Doe", "Maria A. Doe", "Mariia" and the Ukrainian
  form is one card, relation "mother", once her facts reach ten. A short form such as "Mia" has another skeleton and
  stays a card of its own (joining nicknames is the later fix of OD-5 b). A card key is the skeleton of its fullest spelling, so a
  rebuild keeps it.
- **Used at query time only:** a `people` slot naming a relation or one spelling widens to every spelling of the
  matching cards (§5.3). This supplies the spellings the slot model cannot know and closes the relation gap of §1;
  facts are never re-linked or versioned.
- **Shown** in the weekly memory digest (`fact_review_digest.py`) as one bot-written line listing the cards and their
  counts, so the owner sees what the system groups. No edit page in this RFC (OD-5).
- **Lifecycle:** a group that falls below ten facts loses its card on the next rebuild; nothing else is stored.

## 8. Backfill

1. Confirm run (§12 item 1): the production token pair on the 20 probe facts, local, ≈ $0.06; the owner reviews the
   table against the round-4 probe before anything is written.
2. A local, gitignored backfill script (ids from `.env`): takes the account's memory-write lease through the
   port, runs `FactProfileService.run` with no per-run cap in chunks that fit one lease, prints the estimate first
   (facts × $0.003) and the spend, drops and abandoned facts after. The owner approved the backfill budget.
3. The cycle step (PR 4) is already live, so facts written during the backfill are covered.
4. Done when the scan reports 0 `missing`/`stale_input` current facts. Text, metadata and vectors are untouched (the
   repository refuses any other key).
5. **Scope: both live accounts** (owner, 2026-11-09). Ids come from `.env` / local memory, never from tracked files.
   The second account's fact count is measured first and the estimate printed before its run.
6. **Backup (owner, 2026-11-09).** Before the backfill, and again before PR 13, a read-only script exports every fact
   document of both accounts (all versions, all fields, vectors included) to a dated, gitignored local backup directory,
   with per-account counts. A restore script ships with it; it is verified by a dry run that compares the backup with the
   live store field by field and reports differences, never by a real restore. The backup is the reason PR 13 needs no
   waiting period.

## 9. PR series

Each PR is small, deploys on its own and changes no behaviour a later PR needs undone. Tests named per PR.

| PR | Content | Deploy precondition | Tests |
|---|---|---|---|
| 1 | Domain registry, renderers, legacy config map | — | `tests/unit/domain/test_fact_domains_registry.py` (27 keys, enums derived, rows render, legacy map), `tests/unit/test_arch_single_domain_registry.py`, `tests/unit/locales/test_fact_domain_labels.py` |
| 2 | `FactProfile`, `PROFILE_FIELDS`, fingerprint + classify, derive (precedence, label/time/place/people checks, drop counts), repository `list_profile_states`, `write_fact_profile`, entity fields; `label_vector` index | index READY before PR 8 only | `tests/unit/domain/test_fact_profile_derive.py`, `test_fact_profile_fingerprint.py`, `test_fact_profile_validation.py`, `tests/unit/adapters/test_fact_profile_store_kit.py`, `tests/unit/domain/test_profile_fields_disjoint.py` |
| 3 | `FactProfilerAgent`, tokens + snapshot, registry block | tokens uploaded | `tests/unit/agents/test_fact_profiler_agent.py`, `tests/contracts/test_fact_profiler_prompt_contract.py`, wire test per `ADAPTER_WIRE_TESTING.md` |
| 4 | `FactProfileService` step in both leased runs, knobs, logs; backfill script | PR 3 tokens live | `tests/unit/services/test_fact_profile_service.py`, `tests/unit/services/test_consolidation_profile_step.py`, `tests/unit/services/test_fact_review_profile_step.py` |
| — | **Backfill** (§8) | owner OK | — |
| 5 | People cards + digest line | — | `tests/unit/domain/test_person_cards.py`, `tests/unit/services/test_fact_review_digest_people_line.py` |
| 6 | `fact_search_lists.py`, `FactQuery`, `FactSearchService` + port, snapshot cache; no caller | — | `tests/unit/domain/test_fact_search_windows.py`, `test_fact_search_recurring.py`, `test_fact_search_intersection.py`, `test_fact_search_people.py`, `test_fact_search_intimate_gate.py`, `test_fact_search_collect_all.py`, `test_fact_search_kinds_list.py`, `tests/unit/services/test_fact_search_service.py` |
| 7 | P1 token + `FactQuerySlotsAgent` + `parse_slots` | token uploaded | `tests/unit/domain/test_fact_query_parse.py`, `tests/contracts/test_fact_query_slots_prompt_contract.py`, `tests/unit/agents/test_fact_query_slots_agent.py` |
| — | **Acceptance re-run** (§10.3) | backfill done | — |
| 8 | `search_memory` on the new engine, `when:` line | `label_vector` index READY | `tests/unit/agents/test_memory_search_agent_v2.py` |
| 9 | Router prefetch on the new engine | — | `tests/unit/agents/test_router_prefetch_v2.py` |
| 10 | Connector + voice tool | owner reconnects claude.ai if the schema changes | `tests/unit/composition/test_mcp_get_user_context_v2.py` |
| 11 | Baseline identity block, formatter labels, Lelik filter | identity index READY; scan shows 0 unprofiled | `tests/unit/services/test_biographical_identity_block.py`, `tests/unit/services/test_lelik_voice_filter.py`, `tests/unit/services/prompt_v3/test_biographical_formatter_labels.py` |
| 12 | `is_directive` predicate, Cabinet filter + domains API, companion slice, channel-binding load | Cabinet index READY | `tests/unit/domain/test_is_directive.py`, `tests/unit/web/test_cabinet_fact_domains.py`, `tests/unit/services/test_companion_slice_v2.py`, `tests/unit/adapters/test_channel_binding_legacy_domains.py` |
| 13 | Consolidator writes the directive decision only; `FactDomain` and old domain indexes removed; Router triage drops its search fields (OD-6) | consolidation bench dry run clean | consolidation vocabulary tests updated by a reviewer; `tests/unit/agents/test_consolidation_directive_flag.py` |

An existing test that fails under any PR goes to a reviewer (REVIEWER RULE); tests of deleted code are deleted in that PR
and listed in its body.

## 10. Test plan

### 10.1 Behavioural tests on the invented universe

The fixture store is built from universe facts with hand-set profiles (the tests exercise code over stored fields):
Clara Moreira's birth date (14 July 1982) — recurring July match; the Rome trip (10–14 Aug 2025, Albergo Immaginario,
RM-55120) — a `co:italy` query reaches the Rome fact, a `past` window over August 2025 ranks it first; the car
insurance (01/04/2026–31/03/2027) — validity ordering and the `when:` line; Alex's passport (expires 2031-03-04) and
Clara's (valid until 2026-12-09) — the `document` kinds list across domains, `future` window order; Maria Doe's spellings
— one card, a "mother" query reaches a fact naming only "Mariia"; the pollen allergy — a `constraint`. The universe has
one discreet fact — Alex's trade-union membership at Acme (dues 12 EUR/month) — and no intimate one; the discreet tests use
it, and the intimate gate tests set `sensitivity` on a neutral universe fact, since the code reads only the stored field. Derivation tests replay the taxonomy's boundary cases marked `generic` (never the owner-derived ones).

### 10.2 Contract tests for the prompt tokens

- The rendered profiler prompt (tokens + registry block) and the rendered P1 static text each equal their reviewed text
  (sha256 pinned in the test; a wording change is a reviewed edit of the pin).
- No Cyrillic, no example sentences (vocabulary check as `tests/unit/prompts/test_prompt_rules_match_domain.py`); every
  field named in the prompt exists in the JSON schema and the reverse; schema enums equal the registry.
- Profiler output validators (`tests/contracts/adapter_contracts.py` style): a valid answer passes, a missing field or
  an out-of-enum value fails the parse and triggers the one retry.
- Cloud parity (`make test-cloud`): `list_profile_states` projection, the `write_fact_profile` CAS, `label_vector`
  `find_nearest`, array fields round-trip.

### 10.3 Acceptance re-run of the control questions (local, gitignored)

After the backfill, export a snapshot to a gitignored local directory and run the control questions through the **production**
`FactSearchService` and P1 (3 runs, 2-of-3 rule, design and held-out splits, the cluster hold-out frozen earlier),
with people cards at the production threshold. Pass: every required target the prototype reached on 2 of 3 is reached;
set targets within the run-to-run band; each difference explained in the design log. The owner sees the table before
PR 8. A fresh, truly unseen question set, labelled from two weeks of use, runs once after PR 10.

## 11. Rollout

1. PR 1–2 merged; `make deploy-indexes`; wait for READY.
2. Owner uploads the profiler tokens; PR 3 deploys; confirm run on the 20 probe facts.
3. PR 4 deploys; backfill run (owner OK); scan shows 0 unprofiled.
4. PR 5–7 deploy (no reader change); owner uploads the P1 token; acceptance re-run; owner approves.
5. PR 8, then 9, then 10, each watched for a day in `[FactSearch]` lines and turn latency.
6. PR 11 (identity index READY first), PR 12 (Cabinet index READY first).
7. PR 13 right after PR 12, no waiting period (owner, 2026-11-09), preceded by the second backup (§8 step 6). Memory map, multi-vector RRF concept and `capabilities.py` are updated in
   the PR that changes the behaviour they describe.

## 12. Decisions

**Owner, 2026-11-09: every recommendation below is accepted** — OD-1 (a), OD-2 (a), OD-3 (a), OD-4 (a) behind its
measurement, OD-5 (a), OD-6 (a), OD-7 (a), OD-8 (a). The owner also allowed adding ONE discreet example to the invented
universe for the sensitivity tests (§10.1). Delivery: the PR series runs back to back, not spread over time; independent
PRs may run in parallel sessions. OD-6 amended: PR 13 follows PR 12 with no week of waiting — a facts backup covers the
risk (§8 step 6). Backfill and backup cover both live accounts.

**OD-1 Key lists: account snapshot vs Firestore array queries.** (a) Snapshot (§5.2): one projected read per change,
pure code identical to the validated engine, no 30-value limits, no index per list; per-instance memory of a few hundred
small documents. (b) Array-contains-any per list: no snapshot, but five composite indexes, a `time_months` and a
recurring-month key per fact, ordering still in code over whole candidate sets. **Recommend (a)**; revisit past a few
thousand facts per account.

**OD-2 Connector and voice slots.** (a) Server-side P1 on the caller's words — the measured configuration; +≈ 1.1 s p50
per voice search. (b) Typed parameters filled by the client model (claude.ai, Lelik's model) — no extra hop, but slot
quality unmeasured, and a schema change means a reconnect. **Recommend (a)** now; (b) as a measured experiment later.

**OD-3 Router slot call.** (a) Parallel with triage, discarded when `search_intent = none` (no added latency, ≈ $0.001 on
turns that do not search); (b) after triage (+≈ 1.1 s on searching turns); (c) merged into the triage token (unmeasured).
**Recommend (a).**

**OD-4 Advice questions (proposal).** (a) One new slot `asks_advice` with its why-paragraph; code adds the
`preference` + `constraint` kinds list when true — changes the frozen P1, so it is measured on the design set plus
advice questions before adoption; (b) always add a shallow preference/constraint list to every prefetch (more facts
injected on every turn); (c) leave the gap. **Recommend (a)** as PR 7b behind the measurement.

**OD-5 People-card corrections.** (a) Digest line only; (b) a Cabinet "not one person / hide" action. **Recommend (a)**
until a wrong card is seen.

**OD-6 Consolidator domain.** (a) PR 13 in this series: the consolidator decides only "directive or not", the 16-value
domain and its tokens go; (b) a later RFC. **Recommend (a)** one week after PR 12, behind a consolidation bench dry run.

**OD-7 Recurring occasions.** (a) Code rule: a label phrase containing `birthday` or `anniversary`, points spanning at most 31 days (day or
month precision) projected into each year (what the prototype ran); (b) a profiler field (prompt change, re-probe). **Recommend (a).**

**OD-8 Time role as a filter (taxonomy read side).** Neither P1 nor the engine has it; the engine uses the role only for
in-list order. (a) Ship role ordering only; (b) add a slot and measure. **Recommend (a).**

**Inputs found contradictory or under-specified (to confirm):**
1. The profiler prompt draft claims its DOMAIN QUESTIONS are the taxonomy's model rule text byte for byte, but holds the
   round-3 rules; the taxonomy text is now the round-4b hybrid. The token pair of §4.3 combines v4 with the hybrid and was never run as one prompt — hence the
   confirm run of §8 step 1.
2. Birth dates: the indexing rules call identity dates `timeless` with no points; prompt v4 makes a birth an `event` with a
   point. Recurring-occasion matching needs the point; this RFC follows v4.
3. P1 asks for `domain_link`, but the link-aware variant was rejected; the field is parsed and ignored (frozen prompt).
4. Time role as a filter: see OD-8.
5. The prototype's recurring rule is a label-word heuristic; OD-7 makes it explicit.
6. Connector/voice: the indexing rules chose typed parameters, the prototype measured server-side P1 (OD-2).
7. The indexing rules' Appendix B (separate Cloud Task + hourly sweep, ECO model, 16-domain `domains_also`) and Appendix A (owner-confirmed
   cards on a Cabinet page) are superseded by the settled decisions; this RFC follows the settled ones.
8. The prototype derived its roster from every relation-named person; production cards need ≥ 10 facts, so side
   persons get no name widening. The acceptance re-run uses the production threshold.
9. Held-out connector path: required 4/5 after tuning against 5/5 before. The miss is one fact that the slots and labels do not meet; the question was seen during tuning, so it is
   no longer clean evidence. Stated cause → PR 10 proceeds; the acceptance re-run (§10.3) reports it separately.

## 13. Out of scope

The consolidator's duplicate search and the review's neighbour search (stay on `enrich_context`); stopping
`tags_vector`/`metadata_vector` writes and free tags; `type`, `temporal_class`, the fact lifecycle, expiry and janitor;
the drive and Gmail indexes; SYSTEM-account facts; the Router triage token (until PR 13); an LLM re-ranker; graph edges
or one-hop expansion; RRF weights; storing "planned/past"; per-turn Router injection into voice; a card editing page;
discreet-fact rules for shared channels beyond Lelik's baseline; re-profiling on a prompt change (an explicit,
owner-approved run).
