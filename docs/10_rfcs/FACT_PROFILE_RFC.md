# RFC: Fact profile — a per-fact retrieval profile, one domain registry and multi-list memory search

**Status:** Accepted by the owner 2026-11-09 (§12); amended by the owner 2026-11-10 (confirmed people list, needs-review
flag, approximate and yearless dates: §4.5, §4.6, §4.4, §12 OD-9 to OD-11) and again the same day (profile questions to
the owner: §4.7, §12 OD-12, OD-13), and a third time (search v2: a scored ranking in place of the list fusion, the
owner's location in the query slots, one search entry point taking several phrases: §5, §9, §12 OD-14). Implementation
in progress (§9 PR series).
**Owner:** Dmytro Deleur.
**Design inputs:** the design inputs (taxonomy, indexing rules, profiler prompt drafts, code survey, offline prototype)
are private working documents; measured numbers quoted here are aggregates from those runs.

**Settled before this RFC (not reopened here):** one Sonnet 5.5 call per fact (effort high, adaptive thinking, cached
system prompt, JSON-schema output); code computes domains and validates everything the model proposes; a profile
carries the fingerprint of its input and every consolidation cycle profiles the facts whose profile is missing or stale;
seven search lists fused by RRF with the domain list kept whole (the fusion since replaced by a scored ranking, OD-14), collect-all mode, plain domain intersection, time role
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
| `time_points` | ≤ 8 maps `{from, to, precision, source}` | model, each point verified as printed; a date printed without a year takes the year the fact was recorded (§4.4) | time list, ordering, `when:` line |
| `place_keys` | `pl:<geonameid>` (with the large neighbour) and `co:<country>`; `lp:<folded city>` for a place the owner confirmed that the atlas lacks (§4.7) | code from the model's `{city, country, source}` via the atlas and the owner's place answers | places list |
| `places_unresolved` | list of `{city, country}` with no single atlas row and no owner answer that places it | code | the place question (§4.7), reports |
| `people` | list of `{name, role, name_keys, role_keys}` | model names and roles; keys by code (`skeleton_key`, word forms) | people list, people cards |
| `primary_domain` | registry key | code (§3.3) | display, voice filter, baseline grouping |
| `domain_keys` | registry keys: primary, hard extras, ≤ 2 topical extras | code | domain list, Cabinet filter |
| `user_identity` | bool | model answer, stored | baseline identity block |
| `kinds` | subset of the seven kinds | model answer | kinds list |
| `sensitivity` | `ordinary` / `discreet` / `intimate` | model answer | intimate gate, voice baseline |
| `profile_review_needed` | bool | model's `quality` answer, checked by code (§4.6); cleared when the owner answered every item (§4.7) | scan counts |
| `profile_review_items` | list of `{field, mention}`, `field` one of `people` / `time_points` / `places` | model, each mention verified as printed (§4.6); an item the owner answered is dropped (§4.7) | the person question (§4.7) |
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
The confirmed people list (§4.5) is not a fact field: it lives in its own collection and the profiler step never writes it.
Nor are the owner's profile answers (§4.7): one document per account, read by the step, written only from an answer.

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
retry. User message: first, when the account has a confirmed people list (§4.5), the whole list as a block; then
`Fact:\n<text>`, then `\nMetadata:\n` and one `key: value` line per key in sorted key order (lists and objects as
compact JSON). The fingerprint hashes the message without the list block, plus the block of the entries the fact names
(§4.2), so a fact that names no listed person has the message bytes it had before the list existed. No `Today:`, no
reported date: the answer depends on the fact and the entries it names, so a fingerprint holds until one of those
changes. The year of a date the fact prints without one is applied by code after the call (§4.4), never asked of the
model.

### 4.2 Fingerprint and staleness (decided)

- `profile_fingerprint = sha256(message without the list block + the block of the entries the fact names)` — text,
  metadata and the confirmed-list entries the fact names by a name form or a relation word (§4.5). Excluded on purpose:
  tags, `reported_date`, expiry, state, priority, the old domain, and every list entry the fact does not name. None of
  them changes what the model must answer for this fact, and a tags-only or lifecycle-only change, or a list edit about
  a person the fact does not name, must not cost a call. A list edit makes exactly the facts that name the edited entry
  `stale_input`; they are re-profiled by the step's normal order and cap.
- `profile_derivation = DERIVE_VERSION | registry digest | atlas digest | is_directive | recording year`, then
  `| a<answers digest>` when the owner has answered a point this fact's stored answer names (§4.7). A mismatch
  means only code inputs changed: the step re-derives from `profile_answer` without a call. The recording year (§4.4) is
  in it because `reported_date` can be rewritten in place (`IN_PLACE_FIELDS`), and a yearless date must follow it
  without a call. The answers digest covers only the answers this fact names, so an answer re-derives exactly the facts
  it changes, and a fact no answer touches keeps its string.
- `classify_profile(fact)` (pure) → `missing` (no fingerprint) / `stale_input` (fingerprint ≠ hash of the current
  message) / `stale_derivation` / `abandoned` (§4.4) / `fresh`.
- **Scope:** every `state == current` fact of the account being processed, including the history-trail fact a history
  rewrite creates (W5, `fact_review_answers.py:133`): it is current, archival and searchable, so it is profiled like any
  other. **SYSTEM-account facts are out**: the step runs per user account inside that account's cycle, and the SYSTEM
  account never consolidates; its facts are prompt components read by lineage (`prompt_builder.py:62`).

### 4.3 The prompt token

Two Firestore tokens, uploaded before the agent ships: `COGNITIVE_PROCESS_FACT_PROFILER` (the v4 general rules, label
words, time role, time points, places, people, the confirmed people, the quality answer, then the domain questions — the
decided "hybrid": each question's why-paragraph, then its crisp rules) and `OUTPUT_FORMAT_FACT_PROFILER`. The closing
`SUBJECT ENUM` / `USE-AREA ENUM` block is a code-rendered static block from the registry, appended after the tokens; it
ships by deploy. The confirmed people list is per account, so it is never in the system prompt: that text stays one
cached text shared by every account, and the list travels in the user message, which is not cached. Text explains, then
states rules, and carries no examples. No inline fallback: a missing token fails the step (`[FactProfile] token missing`),
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
  1900–2100; a month or a year read from a printed full date is accepted (an approximate time stated around a printed
  date is filed at the month of that date); a date the model writes without a year (`--MM-DD`, `--MM`) is accepted only
  when its source prints no year, and takes the **recording year**: the year of the earlier of the fact's `reported_date`
  and `created_at` (UTC; the earlier, because a document missing `reported_date` reads it as the time of the read), so a
  day and month printed without a year are filed in the year the user said them, never in the year of the run; places —
  printed source, city found in the named country by the atlas (`place_lookup.py`, the table loaded
  by `geo_place_table.py`), one row → keys, else `places_unresolved`; people — printed name or role; a name that is not printed is kept only when it is a confirmed list entry's name, the
  source (the printed relation) is printed, and the person is then stored with `from_list`; review items — the mention
  is printed (`review_mention_not_printed` otherwise), items given with `quality = confident` are dropped
  (`review_items_without_flag`). Every drop is counted per reason in `profile_drops`.
- **Cost:** ≈ $0.003 per fact (measured); steady state is the new and changed facts
  of the month, cents. Spend goes through the execution-scoped billing ledger as agent `fact_profiler`.
- **Observability:** per fact `[FactProfile] fact=<id8> outcome=written|rederived|failed|abandoned drops=… ms=… usd=…`;
  per run `[FactProfile] run scanned=… missing=… stale=… rederived=… written=… failed=… deferred=… flagged=… usd=…`
  (`flagged`: profiles written this run with `profile_review_needed`; the scan counts the flagged current facts);
  `prompt_content` rows and Logfire spans come from the agent's normal call path.

### 4.5 Confirmed people list (owner, 2026-11-10)

A per-account list of people the owner has confirmed, kept per account: the user first, by full name, then
each person with their name, the other forms they are written in and their relation to the user. It exists because a
fact often names a relative by a first name alone or by a relation alone, and a relative filed as a stranger is missed
by every relation question; the model cannot know who a first name is, the owner can.

- **Store.** Collection `{prefix}confirmed_people`, one document per account, doc id `{account_id}`:
  `account_id`, `user {name, forms[]}`, `people [{name, forms[], relation}]` (owner's order kept), `source`
  (`owner_script`; later `review_answer`), `confirmed_at`. One document: the list is read whole on every profile step
  and replaced whole on every confirmation. Not on the account document (that document is overwritten whole from
  `BillingAccount` by the account repository) and not among the people cards (`replace_cards` deletes every card it did
  not build, and a card is never in a prompt).
- **Port.** `ConfirmedPeopleRepository` (`get(account_id)`, `replace(account_id, people, source)`): services cannot read
  Firestore themselves, and a second reader is planned (the consolidator, a later PR); `get` is the one read both use.
- **Writers.** Only the owner's confirmation: a local ops script now, an answer to a review question later. The profiler
  step, the consolidator and the review pass never write it; it is never derived from facts or cards. It is not memory
  and not a fact: nothing of it is written into a fact.
- **What the model reads.** The whole list, as one block before `Fact:`: a title line, then one line per entry (name,
  the other forms, the relation; `the user` for the first). The list is short, and it lets the model resolve an
  inflected form of a name or a mention by relation alone. Its exact bytes are pinned by a test.
- **What the fingerprint holds.** Only the entries the fact names (pure, `src/domain/confirmed_people.py`): an entry is
  named when a word of its name or of one of its forms occurs in the fact's text or metadata as a word, or as the same
  transliteration skeleton in the other script (the evidence test `check_people` uses), or a word of its relation occurs
  (plural and possessive forms count; the user's relation is not a word to look for). Selection over-includes on
  purpose — a shared surname or relation selects every entry carrying it. A list edit then re-profiles only the facts
  that mention the edited person by a name form or by their relation word; facts that mention nobody on the list are
  never re-called by it, and the first list write does not re-profile the store.
- **Rules for the model** (token, §4.3): a mention identifies an entry when what the fact prints about the person (a
  name or form, together with any relation, role or context) fits that entry and no other; a name with a relation or
  role that only one entry has is enough, and so is a relation that only one entry has. A person identified by a
  printed name keeps the name as printed and takes the entry's relation as role. A person identified only by a relation
  takes the entry's full name; code accepts that name although the fact does not print it, when it is a list entry's
  name (never the user's) and the printed relation words are given as the source, and stores the person with
  `from_list` so that cards and search can tell it from a printed name. The user is never a person entry. A mention
  that fits several entries is not resolved by a guess: it is left with what the fact states and marked for review
  (§4.6). Otherwise code keeps a name only when it is printed (`check_people`).
- **Failure.** A list that cannot be read or does not parse ends the step before any classification
  (`[FactProfile] run failed`, `error=True`): classifying without it would mark every fact that names a listed person
  stale and re-profile it without the list. No document is an empty list, not an error.

### 4.6 Needs review (owner, 2026-11-10)

The model states the quality of its answer: `confident` when every person, time point and place it returns is what the
fact states, `needs_review` when at least one could not be decided from the fact and was left open. With
`needs_review` it lists `review_items` `{field, mention}` — the field (`people`, `time_points`, `places`) and the printed
words the open point is about. A fact that leaves a person, a date or a place open is better asked than guessed: a guess
files the fact under the wrong person, time or place and nobody notices.

- **Written but flagged.** A `needs_review` profile is written and used by search like any other; the flag and the
  checked items are stored as `profile_review_needed` and `profile_review_items` (§2.1). It is not a failure and does not
  count towards abandonment.
- **The questions** are §4.7 (PR 5c–5e): a profile question is about a current fact, its options are built from the atlas
  or the list, and its answer corrects the profile by code; PR 5b only stores what it reads.
- **Old answers.** A stored answer written before these fields existed reads as `confident` with no items (schema
  defaults), so re-derivation never needs a call for them.

### 4.7 Profile questions to the owner (owner, 2026-11-10)

A place the atlas cannot place, and a person mention the profiler could not tie to one list entry, are asked of the owner
in chat, never guessed: a guess files every fact that prints the place or names the person under the wrong key, and
nobody notices. Questions are meant to stay rare; one interruption is cheaper than a wrong fact. The owner's answer
corrects the profile by code, without a model call.

**What is asked (an open point).**
- *Place* — a place a current fact's profile keeps in `places_unresolved` (atlas reason `ambiguous`, `not_in_atlas`,
  `not_in_country` or `unknown_country`). One question per distinct place across the account: key = folded city + folded
  country (the atlas's canonical country name when it knows the country), as the profile names them. The answer applies to
  every fact whose profile names that place. The reason and the candidate rows are read from the atlas when the
  question is built (`PlaceNames.candidates`), not stored on the fact (OD-13 d).
- *Person* — a `people` review item whose mention fits at least one entry of the confirmed list (the user included,
  by the selection rule of §4.5). One question per fact lineage and folded mention: the profiler flags a mention exactly
  when it fits several entries, so two facts printing the same words may mean two people (OD-13 a).
- *Date* — a `time_points` review item. One question per fact lineage: a date belongs to one fact (OD-13 c).
- Not asked: a `places` item that is not an unresolved place, a `people` item that fits no entry, a flag without
  items. They stay flagged and counted by the scan.

**One channel.** A profile question is a `FactReviewQuestion` of kind `place`, `person` or `date`, sent through the memory
review's queue, current-question slot, primary channel, session pair, message ref, reply intake and buttons — no second
channel. What differs is stated in the record, so no reader has to guess: origin `profile`, no texts, delivery `chat`,
fallback `none` (silence changes nothing; a lifecycle question can never take `none` and a profile question can take
nothing else), never a `reply_text` (the owner's words are never offered to the review model), `quoted_text` the newest
current fact naming the point, an `open_point` with the key, the printed words, the atlas reason and the options shown,
and an id derived from the account and the key, so one point is never queued twice. Every reader that treats a question
as a stale fact in the review's hands (held facts and the pool, the review batch and the review model's view, the pass's
closes, fallbacks, kept-twice, the digest, the Cabinet queue) reads lifecycle questions only: the store's list reads
return lifecycle questions unless a reader names another scope. Only the claim, the reply and button lookup, the
review's housekeeping (a stuck send, an asked question that becomes unanswered, a timeout) and the janitor's
`review_due` read both.

**Order and rate.** Lifecycle questions go first (a stale fact is wrong in answers now; a profile gap costs recall).
At most one profile question is claimed per owner-local day, inside the shared daily cap, and at most three are open.
They are created at the end of the daily review run, after its profile step: the points named by the most current
facts first, then the newest fact. A run that ends with an error creates and closes nothing; otherwise an open profile
question whose point is gone (answered, placed by an atlas update, its facts no longer current) closes
`resolved_elsewhere`.

**The message.** The question line of its kind and the printed words, the quoted fact (its stored translation when
there is one), the options, the usual reply hint. Place: up to three atlas rows of that name, heaviest first, labelled
by name and country (and the large neighbour where two rows of one country would read alike), then "keep as written"
(a real place the atlas lacks) and "don't remember". Person: up to three fitting entries (name and relation), then "not
on my list" and "don't remember". Date: the printed words, then "don't remember"; the reply names the date. A digit or a
button picks an option.

**Words, read by code only.** A place reply of a city and a known country, a city the atlas places in one row, or a
country that names one shown option; a person reply of a name or form of a shown entry; a date reply in one of the
forms the derivation already accepts from the profiler (a year, a year and month, a full date, a range of two of
these), read by the same parser. Anything else is not
applied: the question stays open, the owner is told what form is read, and nothing is recorded for the review model. No
model reads a reply: the question states the form, replies are short, and a failed read costs one more reply; a model
would add a prompt, its contract tests, a failure path and a cost per reply. If the counters ever show replies that
code cannot read, the rule for a model is at most one cheap-tier call per owner reply, only after code failed to read it.

**Where answers live.** `{prefix}profile_answers`, one document per account (doc id `{account_id}`): `places [{key,
city, country, resolution, geonameid, question_id, answered_at}]` and `people [{key, lineage_id, mention, resolution,
name, question_id, answered_at}]`. Place resolutions: `atlas` (the row the owner picked or named), `local` (a place the
atlas lacks, city and country as printed or typed), `unknown`. Person resolutions: `entry` (an entry's name), `not_listed`,
`unknown`. Port `ProfileAnswerRepository` (`get`, `record` — a transaction, the first answer for a key wins). Per account
and never in the repository atlas (the repository is public), not on the account document, not in the confirmed list
(OD-13 b). Written only from the owner's answer; the profiler, the consolidator and the review pass never write it. A
document that does not parse ends the profile step, as the list does (§4.5): deriving without it would undo answers.
A timeout (14 days) writes nothing; the derived id keeps the point from being asked again while the closed question is
kept (90 days), after which an unanswered point may be asked once more. "Don't remember" records `unknown`: never
asked again.

**Effect without a model call.** `derive_profile` reads the answers the fact names. A place answer is applied before
the atlas: `atlas` gives that row's keys as resolution would, `local` gives `lp:<folded city>` and the country key when
the atlas knows the country, `unknown` leaves the place unresolved; a row that has left the atlas falls back to
resolution and is counted. A `places` review item naming an answered place is dropped. A person answer applies the
rules of §4.5 to that mention: a printed name is kept and takes the entry's relation, a relation alone takes the
entry's full name with `from_list`; the user means no person entry; `not_listed` keeps what the fact prints; the item
is dropped. The flag clears when the owner answered every item the model gave. The answers digest in
`profile_derivation` (§4.2) makes exactly the facts naming an answered point `stale_derivation`; the next profile step
(the next consolidation or daily review) re-derives them free and refreshes the baseline. Search reaches a local place
through the `lp:` key its query side adds (§5.3).

## 5. Search

### 5.1 Query contract and slots

`FactQuery` (`src/domain/fact_query.py`): `subject`, `kind`, `people[]`, `places[{name, country}]`,
`time{intent, from, to}`, `domains` (≤ 2 registry keys), `kinds[]`, `scope` (`top`/`all`), `today`, `path`, and two
inputs that are not slots (OD-14): `message`, the whole text the slots were read from (one phrase of the memory search
agent, or the user's last message on the Router path), embedded for one more similarity; `home`, the owner's location
as their settings store it, resolved by the search into the home town's place keys. `parse_slots` validates in code:
dates parsed and the window clamped (§5.3), unknown domains and kinds dropped, a slot with Cyrillic refused and logged
(the contract is English), `domain_link` read and ignored (§12 item 3).

Slots come from the **P1 prompt** as a Firestore token (`OUTPUT_FORMAT_FACT_QUERY_SLOTS` with its rules; LIFE AREAS
rendered from the registry; `Today: <date> (<weekday>), timezone <owner zone>` appended per call), run by
`FactQuerySlotsAgent` on the tier of its caller — the Router's and `facts_memory`'s ECO tier (flash-lite). The call
carries the owner's location block, and the token ends with one paragraph that explains it: a request about something
coming to the user or happening where they are is about their town even when the request does not name it, so the
slots put that town as the place (OD-14; the frozen head is otherwise unchanged). Inputs: the Router path passes the
turn's messages; the memory search agent passes each phrase it received as `SEARCH_REQUEST "<phrase>"`, one call per
phrase, in parallel (§5.4). A failed slots call degrades to one semantic list over the raw text (no prompt involved).

### 5.2 Where the lists read from

`FactSearchService` (implements `FactSearchPort`, `src/services/fact_search_service.py`) reads two kinds of input:

- the two kNN lists through `find_nearest` (`vector`, `label_vector`), depth 60;
- the five key lists over an **account snapshot**: one projected read of the account's current facts (profile fields,
  text, `metadata`, `context`, `reported_date`, `created_at`; no vectors in the read), cached per instance and keyed by the baseline cache document's
  `refreshed_at`. Every fact writer already refreshes the baseline (memory map §4.1 "When it changes"), so the key moves
  whenever the facts do. The key lists, ordering and gate then run in pure domain code
  (`src/domain/fact_search_lists.py`), the scoring and the phrase fusion in `src/domain/fact_search_rank.py` (§5.3). Text vectors are cached per account by fact id
  (a document's `vector` never changes, so a reload fetches only the ids it has not seen) and serve in-list similarity,
  the message list and its similarity, and deduplication. Label vectors are read with each snapshot load (a re-profile
  rewrites them in place, so they are not kept across loads) and serve the label similarity of every candidate. The
  account's people cards (§7) are read with the snapshot and serve the people score. (OD-1 is the alternative:
  Firestore array queries per list.)

### 5.3 The lists

The lists no longer rank: they generate the candidates the scoring of this section orders (OD-14). Every list keeps its
whole candidate set except where a cap is named; the cut to 30 comes only after scoring.

| List | Runs when | Candidates | Order inside the list |
|---|---|---|---|
| semantic | `subject` | `vector` kNN, 60 | similarity |
| labels | `kind` | `label_vector` kNN, 60; facts without labels excluded | similarity |
| message | `message` | the 60 snapshot facts whose text vector is nearest to the message's | similarity |
| domain | `domains` | every fact whose `domain_keys` hold the domain; two domains → the facts carrying both first, then the facts carrying either | in-list order, capped at 100 |
| home | no `places` and the owner's home resolves | facts whose `place_keys` meet the home town's keys | in-list order, capped at 60 |
| time | intent `future`/`past`/`period` | a point inside the window; a recurring occasion matches by month and day in any year | in-list order |
| time (latest) | intent `latest` | union of the semantic, labels and key lists' candidates with a point (pooled before the message, domain-cap and home lists are built) | newest point first |
| places | `places` | `place_keys` ∩ the atlas query keys (a country reaches its cities; a city reaches its large neighbour) plus `lp:<folded name>` for a named city (an owner-confirmed place the atlas lacks, §4.7) | in-list order |
| people | `people` | a relation word → facts whose mention role has it; a name → facts whose name keys match (either way round); not narrowed by the domains. A card's other spellings raise a fact's people score instead of widening the list (§7) | in-list order |
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

**Intimate gate:** an `intimate` fact survives only in the labels list or the domain list (the message and home lists
are gated like the rest); exception — `scope=all` with people named (collect everything about a person). The
candidates are the union of the gated lists; the Router path drops facts already in the baseline before scoring.

**Scoring (OD-14).** Each candidate gets a linear score, a weighted sum of features computed from the query and the
fact's stored fields; the scorer is pure (`src/domain/fact_search_rank.py`). The features:

- *meaning* — cosine of the subject to the fact text; of the kind to the fact's label vector; of the message to the
  fact text (each 0 when the text is absent);
- *place* — a grade of the fact's place keys against the query places: the named city (or its owner-confirmed `lp:`
  key, or the country when only a country is named) highest, its large neighbour next, the same country low, another
  city negative, none 0; *home unplaced* — the query place is the owner's home town and the fact has no place;
  *home implicit* — the query names no place and the fact is at the home town;
- *kind* — a requested kind among the fact's kinds; *domain* — the fact's primary domain is a queried one (full), or
  only one of its further domains (half); *people* — a queried name matches a mention (full), a mention is another
  spelling on a card of that name (high), the mention's role or its card's relation is a queried relation (medium);
- *time* — the fact's time role (one indicator per role, timeless included); a time point overlapping the target (the
  intent's window, else today); closeness of the nearest point outside it, either side (`exp(−days/30)`); no time
  point at all; an event wholly before today under a `future` intent or wholly after it under `past`; the age of the
  report (`exp(−days since reported/365)`). Recurring occasions measure their distance by month and day under a window
  intent.

The weights were fitted offline by a pairwise logistic model on the calibration set (local, never in the repository)
and live in code as named constants, one line of meaning each; the score is therefore in log-odds units of "this fact
ranks above that one". Candidates are ordered by score (ties by fact id), `SmartDeduplication(0.96, 0.98)` drops
near-duplicates in that order, and the first 30 are kept; `scope=all` keeps every candidate after dedup.

**Several phrases (OD-14).** The memory search agent sends up to four queries, one per phrase, in priority order. Each
is scored on its own candidates; a fact's fused value is the maximum over the phrases that found it of the phrase's
priority weight (1, 0.7, 0.5, 0.35 by position) times σ of its score taken relative to that phrase's own cut line (the
score of its 30th candidate after dedup, or its last when fewer): a fact the phrase ranks at its line reads 0.5, so the
priority weights trade phrases on one scale. No bonus for being found by several phrases (nothing to fit it on). Then
dedup in fused order and the cut to 30; `scope=all` on any phrase keeps every fused fact. One query is the one-phrase
case: its order is its score order. (The cut-line anchor is the author's reading of "σ(score)", to confirm: OD-14.)
One log line per search:
`[FactSearch] path=… phrases=<n> lists=semantic:…,labels:…,message:…,… cands=… gate_dropped=… kept=… unsnapped=… snapshot=hit|load:<n> ms=…`.

### 5.4 Callers

**One entry point (OD-14).** Every model that asks the memory a question — Smart and Quick, Lelik, the claude.ai
connector — goes through the memory search agent (`FactsMemoryAgent`). The request is `queries`: one to four English
phrases in priority order, each about one subject. The reason is the store's shape: a fact is one short English
statement about one matter, so a phrase that reads like such a statement lands next to it, and a phrase holding two
people, two places or two times lands between their facts and near none; a need with several subjects, or one subject
seen from several sides, becomes several phrases. The order is the caller's statement of what the answer depends on
most, which the fusion above turns into weights. The phrases must be self-contained: the search never sees the
conversation. The tool and protocol texts that tell each caller this explain it first, then state the rules, and carry
no examples. There is no other request shape and no fallback to a single free-text query.

| Caller | Slots | Receives |
|---|---|---|
| Router prefetch | P1 call on the turn's messages, started in parallel with triage; used when `search_intent ≠ none` (OD-3 (a)); one query, `message` = the user's last message | ≤ 30 minus the baseline, as `query_specific_context` |
| `search_memory` from Smart, Quick and Lelik (`delegate_to_specialist`, `context.queries`) | P1 per phrase, in parallel; `message` = the phrase | 30 (`scope=all`: all), rendered with `context:`, `reported:`, `metadata:` and a `when: <points at their precision>` line |
| claude.ai connector `get_user_context(queries)` | the memory search agent, as above | 30 / all, grouped by the registry label of `primary_domain`, with `when:` |
| Lelik on a call that carries the MCP servers (voice token) | as the connector | as the connector, then `VOICE_RESULT_MAX_CHARS` (8,000, `voice_mcp_token.py:26`) |
| Companion biographical slice | `subject` from its first phrase, `domains` from `session_domains` | as today |

Each caller passes the owner's location as `home`. `enrich_context` stays for the consolidator's
`search_existing_facts`, the review's neighbour search and the companion slice until PR 12 (out of scope).

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
| 10 | `mcp_setup.py:175-182`, `:361-370` | groups by `f.domain`; calls `enrich_context` itself | the memory search agent with `queries`; registry label of `primary_domain`; gate applied by the engine | 10 |
| 11 | `search_enrichment_service.py:135,363`, `firestore_repo.py:909-953` | domain channel `domain IN`, newest first | domain list of §5.3; old method kept for `enrich_context` users | 8–10 |
| 12 | `router_agent.py:310-338` | prefetch from triage `semantic_lens`/`search_phrase`/`relevant_domains` | P1 slots + `FactSearchService`; the triage search fields are removed in the same PR (OD-14) | 9 |
| 13 | `memory_search_agent.py:108-153, 241-247, 295-352` | `MEMORYSEARCH_*` decomposer, old domains, fallback vector search | `queries` (1–4 phrases); P1 slots per phrase; one multi-phrase search; a failed slots call = one semantic list for that phrase; no legacy payload | 8 |
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
- **Used at query time only:** a fact whose mention is another spelling on the card of a queried name, or on a card
  whose relation is a queried relation word, gets the people score of §5.3. This supplies the spellings the slot model
  cannot know and closes the relation gap of §1; facts are never re-linked or versioned.
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
| 5b | Confirmed people list (store, port, selection, block in the profiler message, fingerprint), needs-review answer and fields, approximate and yearless dates in the derive check; tokens amended | tokens uploaded; the owner's list written by the ops script (dry run first) | `tests/unit/domain/test_confirmed_people.py`, `test_fact_profile_fingerprint_people.py`, `test_fact_profile_yearless_dates.py`, `test_fact_profile_approximate_dates.py`, `test_fact_profile_review.py`, `test_fact_profile_people_from_list.py`, `tests/unit/adapters/test_confirmed_people_store_kit.py`, `tests/unit/services/test_fact_profile_people_list.py`, `tests/unit/agents/test_fact_profiler_agent_people_block.py`, `tests/unit/prompts/test_fact_profiler_prompt_contract_review.py` |
| 5c | Profile answers store + port, atlas candidates and row keys, answers in `derive_profile` and the derivation string, `lp:` key on the query side; nothing writes answers yet | — | `tests/unit/domain/test_profile_answers.py`, `test_place_candidates.py`, `test_fact_profile_derive_answers.py`, `test_fact_profile_derivation_answers.py`, `tests/unit/adapters/test_profile_answer_store_kit.py`, `tests/unit/services/test_fact_profile_answers_step.py`, `tests/unit/services/test_fact_search_local_place.py` |
| 5d | Profile question kinds on the review record, scoped list reads, order and daily rate, housekeeping and timeout, the message and its labels, the answer path for digits and buttons; nothing creates questions yet | — | `tests/unit/domain/test_profile_question_record.py`, `test_profile_question_queue.py`, `test_profile_question_message.py`, `tests/unit/services/test_profile_question_answers.py`, `test_fact_review_profile_housekeeping.py`, `test_fact_lifecycle_sweep_profile_scope.py` |
| 5e | Open points from the profile view, creation and `resolved_elsewhere` in the daily run, words read by code; turns the questions on | 5c and 5d deployed | `tests/unit/domain/test_profile_open_points.py`, `test_profile_reply_words.py`, `tests/unit/services/test_fact_review_profile_questions.py` |
| 6 | `fact_search_lists.py`, `FactQuery`, `FactSearchService` + port, snapshot cache; no caller | — | `tests/unit/domain/test_fact_search_windows.py`, `test_fact_search_recurring.py`, `test_fact_search_intersection.py`, `test_fact_search_people.py`, `test_fact_search_intimate_gate.py`, `test_fact_search_collect_all.py`, `test_fact_search_kinds_list.py`, `tests/unit/services/test_fact_search_service.py` |
| 6b | Scored ranking (OD-14): candidate lists (kNN 60, message list, home list, uncut key lists), the pure scorer and phrase fusion in `fact_search_rank.py`, `FactQuery.message`/`home`, label vectors and people cards with the snapshot, the port taking 1–4 queries; still no caller | — | `tests/unit/domain/test_fact_search_rank_features.py`, `test_fact_search_rank_order.py`, `test_fact_search_candidates.py`, `test_fact_search_phrase_fusion.py`, `tests/unit/services/test_fact_search_service_scored.py` |
| 7 | P1 token + `FactQuerySlotsAgent` + `parse_slots` | token uploaded | `tests/unit/domain/test_fact_query_parse.py`, `tests/contracts/test_fact_query_slots_prompt_contract.py`, `tests/unit/agents/test_fact_query_slots_agent.py` |
| 7c | Slots read the owner's location; the home paragraph appended to the P1 token (OD-14) | token uploaded | `tests/unit/agents/test_fact_query_slots_location.py`; the token's contract pins re-measured by a reviewer |
| — | **Acceptance re-run** (§10.3) | backfill done | — |
| 8 | The memory search agent on the new engine: `queries` (1–4 phrases), slots per phrase, one multi-phrase search, `when:` line; `search_memory` context schema; Smart/Quick and Lelik delegation texts | `label_vector` index READY; the delegation tokens uploaded with the deploy | `tests/unit/agents/test_memory_search_agent_v2.py`, `tests/unit/infrastructure/test_search_memory_queries_schema.py` |
| 9 | Router prefetch on the new engine; the triage search fields and their prompt text removed (moved here from PR 13) | router token uploaded with the deploy | `tests/unit/agents/test_router_prefetch_v2.py`, `tests/unit/agents/core/test_router_triage_without_search_fields.py` |
| 10 | Connector `get_user_context(queries)` through the memory search agent (claude.ai and the voice token) | owner reconnects claude.ai (the schema changes) | `tests/unit/composition/test_mcp_get_user_context_v2.py` |
| 11 | Baseline identity block, formatter labels, Lelik filter | identity index READY; scan shows 0 unprofiled | `tests/unit/services/test_biographical_identity_block.py`, `tests/unit/services/test_lelik_voice_filter.py`, `tests/unit/services/prompt_v3/test_biographical_formatter_labels.py` |
| 12 | `is_directive` predicate, Cabinet filter + domains API, companion slice, channel-binding load | Cabinet index READY | `tests/unit/domain/test_is_directive.py`, `tests/unit/web/test_cabinet_fact_domains.py`, `tests/unit/services/test_companion_slice_v2.py`, `tests/unit/adapters/test_channel_binding_legacy_domains.py` |
| 13 | Consolidator writes the directive decision only; `FactDomain` and old domain indexes removed (OD-6); the Router triage part moved to PR 9 | consolidation bench dry run clean | consolidation vocabulary tests updated by a reviewer; `tests/unit/agents/test_consolidation_directive_flag.py` |

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

Confirmed people cases use the universe household as the list: Alex Doe (forms "Alex", "Bo") as the user, Clara Moreira
partner, Jamie Doe older daughter, Ana Doe younger daughter, Maria Doe mother (forms "Mariia", "Mia"), Hanna Doe sister.
A fact printing "Jamie" names Jamie Doe only; a fact printing only "Doe" names every Doe; "mother" names Maria Doe;
"Mariia" in Ukrainian script names Maria Doe; a fact printing none of them names nobody and keeps its fingerprint.
Profile questions (§4.7) use a fixture atlas: Rome with rows in two countries (the ambiguous place), Porto and Lisbon
with one row each, no Springfield (the place the atlas lacks). Facts printing Rome without a country share one
question; a picked row gives every one of them that row's keys after one free re-derivation, and a fact printing Porto
is untouched. "Keep as written" on Springfield gives `lp:springfield`, and a query naming Springfield reaches it. A
person question over a list of Alex Doe (the user), Clara Moreira and Jamie Doe offers Alex Doe and Jamie Doe for a
flagged "Doe", and its answer applies only to the quoted fact.
Editing Ana Doe's entry makes only the facts that mention Ana (by name or as the younger daughter) stale. Dates:
"Hamburg conference Oct 15" recorded in 2026 gives 2026-10-15; "weekend in Coimbra with Clara Mar 6–8" gives the range
2026-03-06 to 2026-03-08; a conference talk "around mid-October 2026" is filed at 2026-10 (month precision).

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
2b. PR 5b: the owner uploads the amended profiler tokens; PR 5b deploys; the ops script shows the owner's list and the
    number of facts it would make stale, then writes it on the owner's word. The step re-profiles those facts within its
    per-run cap; facts that name no listed person are not re-profiled by this (§13).
2c. PR 5c and 5d deploy dark (no question exists yet; their texts ship by deploy, no token upload); PR 5e turns the
    questions on. The first daily run after it queues at most three; one goes out per day.
3. PR 4 deploys; backfill run (owner OK); scan shows 0 unprofiled.
4. PR 5–7, 6b and 7c deploy (no reader change); owner uploads the P1 token with the home paragraph; acceptance re-run;
   owner approves.
5. PR 8 with its delegation tokens, then 9 with the router token, then 10 (owner reconnects claude.ai), each watched for
   a day in `[FactSearch]` lines and turn latency. PR 8 and its tokens go live together: the agent accepts no other
   request shape.
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
*Amended by OD-14:* slots stay server-side, but the client model no longer passes its raw words: it writes one to four
self-contained English phrases (`queries`), and the server runs P1 on each phrase through the memory search agent.
Typed slot parameters (b) remain unused.

**OD-3 Router slot call.** (a) Parallel with triage, discarded when `search_intent = none` (no added latency, ≈ $0.001 on
turns that do not search); (b) after triage (+≈ 1.1 s on searching turns); (c) merged into the triage token (unmeasured).
**Recommend (a).** *Amended by OD-14:* (a) stays. (c), the triage call emitting the slots itself, was measured on the
calibration set (local) and rejected. With the prefetch on P1, the triage's own search fields have no reader and go in
PR 9.

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

**Owner, 2026-11-10 — amendments, all accepted:**

**OD-9 Confirmed people list.** One owner-confirmed list per account (§4.5), stored in Firestore, never in a token or the
repository; written only from the owner's confirmation; read by the profiler now and by the consolidator later. The
whole list travels with every profiler call; the fingerprint holds only the entries a fact names, so a list edit
re-profiles only the facts that name the edited person.

**OD-10 Needs review.** The profiler states `confident` or `needs_review` with the open points (§4.6); a flagged profile
is written and used; the owner is asked in chat by a later PR rather than the model guessing.

**OD-11 Dates.** An approximate time around a printed date is filed at the coarser precision of that date (its month);
a date printed without a year takes the recording year, applied by code from the fact's own fields, so the answer stays
independent of when the profiler runs.

**OD-12 Profile questions (owner, 2026-11-10, binding).** A place the atlas cannot place (ambiguous, or missing) is
asked of the owner in chat; the biggest namesake is never picked silently. The owner's place answers are stored per
account, never in the repository atlas; filling the atlas in the background is out of scope. Needs-review items use the
same chat channel. An answer fixes the profile without asking the model again where code can do it.

**OD-13 Design choices of §4.7 (decided 2026-11-10 under the owner's delegation).**
- (a) *Person questions per fact, not per mention across the account.* A mention is flagged because it fits several
  entries; an account-wide answer would file every other fact printing those words under the person this one meant.
  Places stay account-wide: a printed city and country name one place for the owner.
- (b) *No list edit from an answer in this series.* An alias added to an entry would re-profile every fact naming
  that entry (a model call each) and, for a mention that fits several entries, misfile the others; a new entry needs a
  name and a relation the chat cannot check. The list stays owner-script only; `review_answer` stays reserved.
- (c) *Time-point items are asked, one question per fact.* The owner's rule is that everything the profiler could not
  decide goes to chat. A date answer belongs to one fact, so it is stored for that fact's lineage. The reply is read only
  in the date forms the derivation already parses; a reply in other words is not applied and the question says which
  form is read. No model and no new free-word date parser.
- (d) *The atlas reason is not stored on the fact.* The question needs the candidate rows anyway, read from the same
  atlas when it is built; storing the reason would change the stored shape and re-derive every profile once for a
  value nothing else reads.

**OD-14 Search v2 (owner, 2026-11-10, all five accepted).** Measured offline on the calibration set (local); no
measured value enters this repository.
- (a) *Scored ranking.* Equal-weight RRF over membership lists was nearly flat, so it degenerated into a vote by how
  many lists named a fact, and a key list's inner order is time, not relevance. The lists now only generate candidates
  (§5.3 table, no cut before scoring); a linear score over the features of §5.3 orders them; the cut to 30 follows.
  The scorer is pure domain code; its weights are constants fitted offline. The message similarity costs one more
  query embedding, batched with the others.
- (b) *Slots know where the owner lives.* The P1 call carries the owner's location block, and one paragraph appended
  to the token explains why an implicit "to me / here / near home" request takes the home town as its place. Model
  unchanged (the cheap tier); no Cyrillic path (a Cyrillic slot is still refused).
- (c) *One entry point.* Smart, Quick, Lelik and the claude.ai connector all call the memory search agent with
  `queries` (§5.4); the connector no longer calls `enrich_context` itself. No backward compatibility and no old
  request shapes (one active user).
- (d) *Several phrases.* Each phrase has its own slots and score; fusion is the maximum over phrases of priority
  weight times σ(score); no multi-phrase bonus; then the cut. **To confirm:** the score is a pairwise log-odds with no
  intercept, so σ of the raw score saturates near 1 for every candidate and the fusion would reduce to "phrase 1
  first, wholly". §5.3 reads σ(score) relative to the phrase's own cut line (the probability that the fact ranks above
  the phrase's 30th candidate); the owner confirms this reading before PR 6b lands.
- (e) *Router.* OD-3 (a) stays; (c) was measured and rejected; the triage drops its search fields in PR 9.

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
the drive and Gmail indexes; SYSTEM-account facts; the Router triage token beyond removing its search fields (PR 9); an LLM re-ranker; graph edges
or one-hop expansion; refitting the ranking weights online or per account; storing "planned/past"; per-turn Router injection into voice; a card editing page;
discreet-fact rules for shared channels beyond Lelik's baseline; re-profiling on a prompt change (an explicit,
owner-approved run); questions about time-point items, list edits from an answer, correcting a recorded answer, profile
questions in the Cabinet, filling the atlas from answers (§4.7, OD-13); the consolidator reading the
confirmed list (a later PR); re-profiling facts that name no listed person after the token amendment (an explicit,
owner-approved run, as for any prompt change); building people cards from the confirmed list.
