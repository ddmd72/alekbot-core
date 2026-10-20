# Drive transcripts of audio and video, slices T1–T3 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status: PLAN ONLY.** Nothing below is executed, deployed or written to the cloud until the owner says so.
- **Agents write code and tests only.** No `gcloud`, no `bq`, no `make deploy`, no `--apply` of any script, no Firestore
  write, no live Gemini/Graph call. Every cloud step is an **[OWNER]** step.
- **No paid model calls by agents.** The T1 eval gate (§27.14 "Eval") is **owner-run, live, cents**: this plan builds
  the fixture generator and the eval runner and unit-tests them with stubs; nobody but the owner runs them against
  Gemini.
- **The feature deploys OFF for everyone.** The global kill switch defaults off and `UserBotConfig.drive_transcribe_enabled`
  defaults False; enabling it for the owner is a morning owner step (§Owner steps 7). The owner's son stays off until the
  sandbox's media slice exists (sandbox RFC S5, out of scope here).

**Goal:** The indexer transcribes the owner's audio and video files in the background (Gemini Flash on the Batch API,
newest first, under a hard $20/month cap), stores each transcript once per content hash, indexes it as the recording's
readable content (T1), projects it as Markdown into one bot-owned mirror folder (T2), and adds a machine-written summary
and header polish (T3). Nothing is asked of the user and nothing is posted to chat.

**Spec:** `docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md` §27 rev 3.25 (T0 passed, GO); `§N`, `Δn`, `Dn` below refer to it.
Sections it builds on: §7.1/§7.3 (tick, quiet period), §8.1/§8.2 (runs, outcome classes), §9 + §24.3 + §24.14 (parser
sandbox model and its owner-accepted residual), §24.7/§25.4.5/§25.4.6 (budgets, enrichment shares, per-agent backoff),
§25.2/§25.3 (keys, `PROFILE_POLICY`), §26.2 (inaccessible files). T0 numbers: `scripts/memory/t0_transcribe/REPORT.md`
(private, gitignored; read for SDK shapes, never copy its identifiers, file names or transcript text into git).

**Current code the slices extend (read before each task):** `src/services/drive_indexing_service.py` (tick),
`src/domain/drive_index_policy.py`, `drive_index_planner.py`, `drive_enrich_plan.py`, `drive_enrichment.py`,
`drive_index.py`, `drive_card.py`, `drive_profile.py`, `drive_search.py`, `drive_extraction.py`, `drive_index_params.py`,
`src/adapters/firestore_drive_index_repo.py` + `in_memory_drive_index_repo.py`, `src/ports/drive_index_repository.py`,
`src/adapters/subprocess_content_extractor.py` + `src/adapters/drive_parser/child.py`, `src/adapters/gemini_adapter.py`,
`src/composition/drive_model_setup.py` + `drive_reader_runner.py` + `drive_agent_build.py`,
`src/infrastructure/drive_model_call.py`, `src/domain/billing.py` + `scripts/validation/price_consensus.py`,
`src/ports/user_drive_port.py` + `src/adapters/microsoft/onedrive_adapter.py`, `src/domain/user.py` (`UserBotConfig`),
`indexer_main.py`, `cloudbuild-dev.yaml`, `Makefile`, `Dockerfile`, `src/agents/file_management_agent.py`,
`src/services/drive_search_service.py`, `src/web/static/cabinet.html`, `docs/05_building_blocks/user_drive/README.md`.

---

## Execution model

- **One branch, one PR:** `feat/drive-transcripts` (this worktree, off `main`). T1, T2 and T3 land as ordered commit groups on
  the same branch; the owner merges once the whole branch is green (memory `feedback_merge_only_when_fully_done`,
  `feedback_owner_merges_prs`). The PR carries its docs. Commit/push on the feature branch is allowed; never `main`, never
  `--no-verify`.
- **Subagent-driven:** per task a fresh implementer (TDD: write the new tests, see them fail, implement, see them pass,
  `make check`, commit), then a fresh reviewer (spec compliance against §27 and Part C as resolved, layering, secrets, logger
  only, tests can fail, no existing test weakened). Max 3 fix rounds, else the task is blocked and logged. Sonnet implements;
  Opus reviews (memory `feedback_subagent_models`, `feedback_code_via_subagents`).
- **Part C must be resolved first.** Every task that touches an open point names it (`G<n>`). The arbiter (the session that
  dispatches this plan) records the resolution of each G-item in the PR description before the first task that depends on it
  is dispatched; an implementer never decides a G-item.
- **Reviewer rule (CLAUDE.md, Tests):** the implementer never edits an existing test. A failing existing test is recorded
  (name, assertion, expected, actual) and handed to the reviewer, who decides per test. Each task lists its **expected-risk
  tests**; anything failing outside that list is presumed a code bug. New behaviour gets new test files.
- **No cloud writes by agents.** Owner steps are marked **[OWNER]**.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (Sonnet implementers use their own
  model line).

## Global constraints

- **SECRETS RULE (public repo).** No project id, service URL, service-account email, user id, account id, drive item id,
  file name or transcript text from the owner's drive in any tracked file, fixture or comment. Test fixtures are synthetic.
  The T0 raw outputs under `scripts/memory/` stay private; wire-test fixtures are hand-built from the SDK types, never copied
  from those files.
- **Privacy of speech in logs.** Transcripts are other people's speech (§27.12). Never log transcript or summary text, a
  Graph download URL, a Gemini file URI, or ffmpeg's stderr text; log lengths, counts, hashes and states only (the
  `_stderr_digest` precedent, `subprocess_content_extractor.py:379-387`).
- **Config reads (CLAUDE.md, `config.get()` vs `os.getenv()`).** `DRIVE_TRANSCRIBE_ENABLED` is an optional knob read with
  `os.getenv` in `indexer_main.py` and passed into the service as a constructor flag; it is never registered in
  `load_settings()`. It reaches Cloud Run as a Cloud Build substitution with its own `.env` key (G13).
- **Layers.** `src/domain/drive_transcript*.py`: stdlib only, pure, no clock, no I/O, no logging, `mypy --strict`. New ports
  (`src/ports/media_audio_port.py`, `src/ports/batch_transcription_port.py`): domain + stdlib + ABC, `mypy --strict`.
  Adapters import `domain/`, `ports/`, `config/` and nothing across subpackages (REQ-ARCH-08) or any other `*_adapter.py`
  (REQ-ARCH-23); adapters never import services/agents/composition/web (REQ-ARCH-16). Services take ports by constructor and
  never import another service (REQ-ARCH-22; `TYPE_CHECKING` imports only). `composition/` never imports `web/`
  (REQ-ARCH-11). Agents inherit `BaseAgent`.
- **One public class per file**, except the stated waivers already used by the drive domain ("several value types of one
  contract in one module", precedent `drive_enrichment.py:10-12`); each waiver is stated in the module docstring.
- Files over ~600 lines put the public interface first (CLAUDE.md "File Size Convention").
- Logging via `src.utils.logger`; no `print()`; every `except` logs before handling or re-raising.
- All code, comments, docs and log lines in English.
- `make check` green after every task (full suite, xdist). Docker-gated tests (`pytest.mark.docker`) are skipped by
  `make check` and run once at acceptance.

---

# Part A — T1: extraction, batch, store, index (no mirror, no summary)

**Scope (§27.14 T1):** `Admission.TRANSCRIBE`, `POLICY_VERSION` 3, the record fields and the job documents,
`MediaAudioPort` + child ops + hardening, `BatchTranscriptionPort` + `GeminiBatchAdapter`, `DriveTranscriberAgent`,
windows, stitching and speaker alignment (T4 folded in, Δ123), the transcript store and content-hash cache, cost +
`UsageClass.TRANSCRIBE` + hard cap with reservations, the card segment, `open_file_from_drive` on a transcribed ref, the
skill and capabilities lines, the kill switch and the per-user flag, the disclosure text. Plus `UserDrivePort.download_url`
(moved into T1, G1).

**Module map (new files, decided here so tasks can refer to them):**
- `src/domain/drive_transcript.py` — value types: `MediaProbe`, `WindowSpec`, `Segment`, `WindowTranscript`,
  `StitchedTranscript`, `TranscriptStub` (enum of the five stub values as strings), `TranscriptStore` (the stored
  structured result), `TranscriptJob`, `JobWindow`, `TranscriptBatch` (waiver: one contract, several value types).
- `src/domain/drive_transcript_time.py` — `MM:SS`/`H:MM:SS` parse and format.
- `src/domain/drive_transcript_windows.py` — window plan, coverage check, time validator, content key.
- `src/domain/drive_transcript_stitch.py` — rebase, seam cut, speaker alignment, interpolation.
- `src/domain/drive_transcript_render.py` — the indexed body, the card segment text, the open-file text.
- `src/domain/drive_transcript_plan.py` — the job state machine and the budget decisions.
- `src/ports/media_audio_port.py`, `src/ports/batch_transcription_port.py`.
- `src/adapters/drive_parser/media_extractor.py` (`SubprocessMediaExtractor`), `src/adapters/drive_parser/media.py`
  (child-side ffmpeg/ffprobe argv and runner), `src/adapters/drive_parser/media_launch.py` (launcher helpers, G15).
- `src/adapters/gemini_batch_adapter.py` (`GeminiBatchAdapter`).
- `src/agents/drive_transcriber_agent.py` (`DriveTranscriberAgent`); `src/composition/drive_transcript_runner.py`
  (`DriveTranscriptRunner`).
- `src/services/drive_transcript_stage.py` (`DriveTranscriptStage`, called by the tick through a host protocol; see Task 12).

## Task 1: Policy 3, record and sync-state fields, planner gates, codecs

**Files:**
- Modify `src/domain/drive_index_policy.py` — `Admission.TRANSCRIBE = "transcribe"`; `TRANSCRIBED_KINDS = frozenset({"audio",
  "video"})`; `admission()` returns `TRANSCRIBE` for them, read at call time like the other two sets (`:88-94`). No change to
  `_EXT_KIND` or `size_cap` (the media caps are §27.4's, applied by the transcript plan, Task 4).
- Modify `src/domain/drive_index.py` — `POLICY_VERSION` 2 → 3 with its comment; **no** `CARD_VERSION` bump (G5) and **no**
  `render`/`summary` keys in `CURRENT_VERSIONS` (G6) unless the arbiter resolves otherwise. `TRANSCRIBE_VERSION = 1` (not in
  `CURRENT_VERSIONS`, by decision Δ122). New `DriveIndexRecord` plain fields (defaults neutral, plain strings, no new enum
  value, Δ47): `transcript_state: str = "none"`, `transcript_key: str = ""`, `transcript_version: int = 0`,
  `transcript_wake_at: Optional[datetime] = None`, `transcript_attempts: int = 0`, `transcript_hold: Optional[str] = None`,
  `transcript_hold_version: str = ""`, `transcript_stub: str = ""`, `transcript_seconds: int = 0`,
  `transcript_speakers: int = 0`, `transcript_languages: Tuple[str, ...] = ()` (the card's `uk/en`, §27.11; not in the RFC's
  field list — named here so the card needs no store read), `transcript_body_version: int = 0` (G18). New `DriveSyncState`
  plain field `purge_keep_transcripts: bool = False` (§27.8 b). The lean projection (`drive_index_lean.py`) is **not**
  widened.
- Modify `src/domain/drive_index_planner.py`:
  - `classify` (`:211-225`): `Admission.TRANSCRIBE` behaves like `CONTENT` without the download size cap (the 8 GiB cap is
    the transcript plan's, Task 4): an outcome that stands is kept, else `PENDING`.
  - `content_due` (`:575-585`) returns False for `TRANSCRIBE` kinds: the media records never reach `_index_step` or
    `chain_for` (§27.3, Δ95). `triage_due` unchanged.
  - `extractor_stale` (`:753-759`): a record whose `kind` is in `TRANSCRIBED_KINDS` and whose `extracted_by` link is
    `transcribe-gemini` is **never** extractor-stale, whatever its version (G4 — otherwise `chain_for("audio")` = `(PARSER,)`,
    `drive_extraction.py:32-33`, makes every transcribed file stale on every upgrade pass, an endless re-finalize loop).
    The same holds at the access-regained path (`drive_indexing_service.py:2249-2250`), which calls the same function.
  - Every transcript outcome that is not an index (the `too_large`/`too_long`/`no_audio`/`no_speech` stubs, `failed`) is
    written through the existing `demoted` / `content_retry` (they stamp `extraction_version`), so `_outcome_stands`
    (`:254-265`) keeps it on a rename or a re-plan; a classify-time `metadata_only` carries `extraction_version=0`
    (`_build`, `:305-311`) and would not stand.
- Modify `src/adapters/firestore_drive_index_repo.py` — `_RECORD_READERS` gains `transcript_wake_at: _dt`,
  `transcript_languages: _strs`, `transcript_hold: _optional(str)`; `_STATE_READERS` gains `purge_keep_transcripts: bool`.
  `in_memory_drive_index_repo.py` needs no codec change (dataclasses) — verify.
- Tests (write first): `tests/unit/domain/test_drive_transcript_policy.py`,
  `tests/unit/domain/test_drive_transcript_planner_gates.py`, `tests/unit/adapters/test_firestore_drive_transcript_fields.py`.

**New tests:**
- `test_audio_and_video_admit_transcribe`, `test_other_media_kinds_unchanged` (image stays `TRIAGE`), `test_admission_reads_sets_at_call_time`.
- `test_media_classifies_pending_under_policy_3`, `test_media_over_45_mib_is_not_too_large_by_download_cap` (the media cap
  is not the download cap), `test_media_outcome_that_stands_is_kept_on_rename`.
- `test_content_due_false_for_media` (pending, not indexed), `test_schedule_gives_media_no_content_work` (only card work).
- `test_transcribe_link_never_extractor_stale` (any version), `test_parser_link_on_audio_still_stale` (a record wrongly read
  by the parser under old code is still re-planned).
- `test_upgrade_pass_reclassifies_media_metadata_only` (service-level, `InMemoryDriveIndexRepository`: a policy-2 media
  record `metadata_only/kind_not_admitted` becomes `pending` with no download, no extractor call, §12.3, Δ96).
- Codec: `test_transcript_fields_roundtrip`, `test_purge_keep_transcripts_roundtrip`, `test_old_doc_without_fields_reads_defaults`.

**Expected-risk existing tests (reviewer decides):**
- `tests/unit/domain/test_drive_index_planner_content.py::...::test_a_video_stays_metadata_only` (`:85`, pins video as
  `kind_not_admitted`; §27 intentionally changes it).
- `tests/unit/services/test_drive_index_hostile_tree.py` (`:73`, `"Media/voice memo.m4a": (N, Reason.KIND_NOT_ADMITTED)`).
- `tests/unit/domain/test_drive_index_policy_admission.py::TestAfterTheFlip::test_each_kind_gets_its_admission[audio]`
  and `[video]` (`:24-28`, expects `Admission.NONE` for every kind outside content/image — a certain break, §27
  intentionally changes it); `tests/unit/domain/test_drive_index_policy.py` only if it pins admission per kind.
- `tests/unit/adapters/test_firestore_drive_index_repo_serialization.py::TestRecordSerialization::test_fixture_sets_every_field`
  and the sync-state twin (`:112-116`, `:154-158`): the shared `full_record()`/`full_state()` helper must set every field —
  new fields break it; the reviewer extends the helper.
- `tests/unit/scripts/test_drive_eval_*` and `scripts/validation/drive_eval` gold cases holding media records (§27.14's
  "expected to break" list).

**Acceptance:** `mypy --strict` clean; with the kill switch off, a full tick over a drive with media changes those records
to `pending` and nothing else (no extractor, no model, no Graph content call).

## Task 2: Domain — transcript types, time strings, window plan, validators, content key

**Files:**
- Create `src/domain/drive_transcript.py` (types listed in the module map; frozen dataclasses; plain strings for states).
- Create `src/domain/drive_transcript_time.py` — `parse_stamp(s) -> float` accepting `M:SS`, `MM:SS`, `H:MM:SS` (seconds
  0–59, minutes 0–59 under an hour component; anything else `ValueError`), `format_stamp(seconds) -> str` (`MM:SS` below one
  hour, `H:MM:SS` from one hour; never integer seconds, Δ128).
- Create `src/domain/drive_transcript_windows.py` — `plan_windows(duration_s, window_s=TRANSCRIBE_WINDOW_S,
  overlap_s=TRANSCRIBE_OVERLAP_S) -> Tuple[WindowSpec, ...]` (window k starts at `k*(window_s-overlap_s)`, the last window is
  short, a file shorter than one window is one window, no zero-length window); `coverage_ok(window, segments, no_speech)`
  (last segment start ≥ `TRANSCRIBE_COVERAGE_MIN` × window duration, a `no_speech` window always ok, Δ129);
  `times_ok(window, segments)` (non-monotonic by > 5 s or past the window length by > 10 s fails, §27.6 "Time sanity");
  `content_key(rec) -> Tuple[str, bool]` = `rec.content_hash` (`quickXorHash:size`, stamped by the planner's `_build` from
  `content_hash_of`, `drive_card.py:68-70`; `content_hash_of` takes a `DriveChange`, not a record) or
  `item:{item_id}:{rec.content_version}` (the record's `content_version` is the cTag, `onedrive_adapter.py:183`) with
  `shared=False` (no sha1 fallback, Δ135).
- Modify `src/domain/drive_index_params.py` — every §27.18 parameter under its RFC name, one comment each citing its basis
  (`TRANSCRIBE_USD_PER_MONTH` 20.0, `TRANSCRIBE_HOUR_USD_ESTIMATE` 0.20, `TRANSCRIBE_WINDOW_S` 1800, `TRANSCRIBE_OVERLAP_S` 60,
  `TRANSCRIBE_COVERAGE_MIN` 0.9, `TRANSCRIBE_MAX_SECONDS` 14 400, `TRANSCRIBE_MAX_BYTES` 8 GiB, `TRANSCRIBE_EXTRACT_WALL_S`
  240, `TRANSCRIBE_CHILD_CPU_S` 150, `TRANSCRIBE_SHARE` 0.25, `TRANSCRIBE_REREAD_COOLDOWN` 24 h, `BATCH_POLL_FIRST_S` 150,
  `BATCH_POLL_EVERY_S` 300, `BATCH_POLLS_PER_TICK` 10, `BATCH_SLOW_AFTER_S` 3600, `BATCH_POLL_SLOW_S` 1800,
  `TRANSCRIBE_MAX_GAP_S` 75, `INTERPOLATED_MAX_SHARE` 0.3, `STITCH_MIN_MATCHED_WORDS` 6, `SUMMARY_MAX_CHARS` 1200,
  `SUMMARY_INPUT_CHARS` 60 000, `TRANSCRIPT_DOC_BYTES` 900 000, `TRANSCRIPT_ORPHAN_TTL` 30 days, `MIRROR_OPS_PER_TICK` 40,
  `MIRROR_SCAN_PAGE` 100, `MIRROR_AUDIT_DAYS` 7, `MIRROR_NAME_MAX` 240, `MIRROR_PATH_MAX` 380, `TRANSCRIBE_UPLOAD_S` (the
  upload worst case of one window, needed for the deadline sum; initial 60, G16), `TRANSCRIPT_OPEN_MAX_CHARS` 60 000 (G19;
  not an RFC name, stated as such in its comment)).
- Tests: `tests/unit/domain/test_drive_transcript_time.py`, `test_drive_transcript_windows.py`.

**New tests (property tests with `hypothesis` where the suite already uses it, else exhaustive small grids):**
- Time: `test_parse_mm_ss`, `test_parse_h_mm_ss`, `test_rejects_integer_seconds_and_garbage` (`"75"`, `"1:75"`, `"-1:00"`),
  `test_format_switches_at_one_hour`, `test_roundtrip_property`.
- Windows: `test_boundaries_and_overlap`, `test_last_window_short`, `test_short_file_one_window`, `test_no_zero_length_window`,
  `test_windows_cover_the_whole_file_property`, `test_45_min_window_allowed` (the parameter range 30–45 min, Δ129).
- Coverage: `test_short_answer_is_not_covered`, `test_no_speech_window_always_covered`, `test_exactly_at_threshold`.
- Validator: `test_non_monotonic_beyond_5s_fails`, `test_small_jitter_passes`, `test_start_past_window_plus_10s_fails`.
- Content key: `test_quickxor_key`, `test_no_quickxor_falls_back_to_item_ctag_unshared`, `test_sha1_is_never_used`.

**Expected-risk existing tests:** none (new modules; params file gains names only). Any `drive_index_params` pin test that
enumerates all names would be the reviewer's.

**Acceptance:** `mypy --strict` clean; no import outside stdlib + `drive_*` domain modules.

## Task 3: Domain — stitching, speaker alignment, interpolation, body render

**Files:**
- Create `src/domain/drive_transcript_stitch.py` — `rebase(window, segments)` (`abs = window.start + start`); `cut_seams(windows)`
  (per adjacent pair, the cut is the overlap's midpoint; window k keeps segments starting before it, k+1 from it on; a segment
  crossing it belongs to the window it starts in); `align_speakers(windows) -> (segments, seams_uncertain: Tuple[bool, ...],
  speaker_count)` — inside each overlap, match labels by normalised-text token overlap and time proximity, rename a matched
  label of k+1 to k's, give an unmatched label the next free number, mark a seam uncertain below `STITCH_MIN_MATCHED_WORDS`
  (Δ109); `interpolate(segments)` — where consecutive starts of one speaker are > `TRANSCRIBE_MAX_GAP_S` apart, split the
  segment at sentence boundaries by character proportion and mark the inserted stamps interpolated; returns the share of
  interpolated stamps per window (`INTERPOLATED_MAX_SHARE`, §27.6).
- Create `src/domain/drive_transcript_render.py` — `render_body(transcript) -> str`: one paragraph per segment,
  `[MM:SS] Speaker N: text` (single speaker: no label; interpolated: `[~MM:SS]`; content-policy gap: `[MM:SS] [this part could
  not be transcribed]`), language-neutral labels (G17), one blank line between segments so the chunker's paragraph split puts a
  stamp at every chunk head (§27.11); `card_segment(rec) -> Optional[str]` (`transcript: 47 min, 2 speakers, uk/en`; a stub:
  `transcript: none (no speech detected)` / `(too long: 5:12:00)` / `(too large)` / `(no audio track)`); `open_text(store, *,
  max_chars)` (G19). Every stub has a deterministic structural rendering here; the localized one-sentence mirror file is T2
  (Task 19). `TRANSCRIPT_BODY_VERSION = 1` lives in this module (G18): it versions `render_body`, the indexed layout.
- Tests: `tests/unit/domain/test_drive_transcript_stitch.py`, `test_drive_transcript_render.py`.

**New tests:**
- Rebase/seams: `test_rebase_adds_window_start`, `test_seam_cut_at_overlap_midpoint`, `test_crossing_segment_belongs_to_its_start`,
  `test_no_duplicate_and_no_loss_at_seams_property` (synthetic overlapping windows built from one script: every script line
  appears exactly once after the cut).
- Alignment: `test_matched_labels_renamed`, `test_unmatched_label_gets_next_number`, `test_speaker_silent_through_overlap_becomes_new`,
  `test_uncertain_seam_below_min_matched_words`, `test_single_window_never_uncertain`, `test_alignment_is_deterministic`.
- Interpolation: `test_gap_over_75s_gets_tilde_stamps`, `test_interpolated_share_reported`, `test_no_interpolation_below_gap`.
- Render: `test_body_format_and_stamp_at_paragraph_heads`, `test_single_speaker_has_no_labels`, `test_gap_marker_render`,
  `test_card_segment_for_each_stub`, `test_render_is_language_neutral`, `test_open_text_cut_with_note`.
- Gate fit: `test_rendered_transcripts_pass_text_gate` — a rendered two-speaker 40-minute synthetic transcript and a
  single-speaker one are **not** `is_machine_text` (`drive_text_gate.py` strips leading stamps and speaker tags); a looping
  hallucination (one line repeated 300 times) **is** (§27.8 a). If the first fails, stop and report: it is a G-item, not a gate
  tweak.

**Expected-risk existing tests:** none.

**Acceptance:** pure, `mypy --strict`; the de-dup property holds for overlaps 10–120 s.

## Task 4: Domain — job state machine and budget decisions

**Files:**
- Create `src/domain/drive_transcript_plan.py` (pure) — the per-record job plan, run on every record write next to
  `enrich.planned` (Task 14 wires it into `_commit`):
  - `transcript_plan(rec, ctx) -> rec` with `ctx` carrying `now`, `enabled` (kill switch AND per-user flag), month dollars
    (`cap_usd("transcribe")`, reserved, cap). **No store lookup in `_commit`:** the plan runs on every record write, and a
    Firestore read of `drive_transcripts` per media write would put a new read (and, with the feature OFF, a new failure
    mode) on the default path. A store hit is found by the stage (Task 12 step 1), which then writes `current`.
  - The state table (G21 for the vocabulary), first match wins: `none` (not a `TRANSCRIBED_KINDS` record, not `normal`
    scope, no readable item, inaccessible §26.2); `current` (transcript or stub at this content key and body version);
    `failed` (`transcript_attempts ≥ MAX_ATTEMPTS` for this content version: ffmpeg could not read it, or windows failed
    repeatedly; no stub, Δ100); `held` (a recorded hold for this content version: `cooldown` — edited within
    `TRANSCRIBE_REREAD_COOLDOWN` of its last transcription; `quiet` — dirty and not `quiet_over`; `transcript_wake_at` set);
    `parked` (the whole file's estimate does not fit the month's cap after pending reservations: `transcript_wake_at` = next
    month start; never cut, Δ106 — **entered by the stage after the probe**, Task 12 step 4, because an audio file has no
    duration before it; the plan only keeps it until the wake-up); `waiting` (windows submitted, open batches); `working`
    (some windows extracted or uploaded, not all submitted); `ready` (due).
  - **A model backoff is not a record state** (§25.4.6 as `JobState`'s docstring reads it, `drive_enrichment.py:36-38`): the
    `drive_transcriber` backoff gates the stage's queue read; records stay `ready`, so an outage writes no record (G21).
  - Stubs decided from metadata where possible: `size_bytes > TRANSCRIBE_MAX_BYTES` → `too_large` stub, `current`, record
    `metadata_only / too_large` (no probe, no model); duration over `TRANSCRIBE_MAX_SECONDS` or `no_audio` come from the probe
    (Task 12) through `stub_from_probe(probe)`.
  - `estimate_usd(seconds) = seconds × TRANSCRIBE_HOUR_USD_ESTIMATE / 3600`; `fits(estimate, ctx)` = `estimate ≤ cap −
    cap_usd − reserved` (the initial build never counts, G8). The file's estimate is **reserved at the moment it fits**
    (before its first window is extracted), not at submission (G39): the fit check and the reservation are one step, so
    two files extracted across several ticks can never both pass a check the cap only holds for one.
  - `reset_on_new_version(rec)`: a new `content_version` clears attempts and holds and re-keys (D42: a new version
    re-transcribes, or hits the cache).
  - Derived queue fields (like `enrich.derived`): `transcript_state == "ready"` is the queue predicate; `transcript_wake_at`
    the wake-up; `effective_day` is already derived by `enrich.derived`.
- Tests: `tests/unit/domain/test_drive_transcript_plan.py`.

**New tests:** `test_ready_to_working_to_waiting_to_current`, `test_kill_switch_off_is_none_work` (state stays planned but
no transition to `working`), `test_per_user_flag_off_same`, `test_cap_hold_parks_the_whole_file` (estimate 0.06, 0.05 left →
parked, nothing partial), `test_pending_reservations_count_against_cap`, `test_month_boundary_wakes_parked`,
`test_new_content_version_resets_attempts_and_holds`, `test_edit_cooldown_holds_24h_after_last_transcription`,
`test_quiet_period_holds_a_recording_still_uploading`, `test_failed_at_max_attempts_has_no_stub`,
`test_too_large_by_metadata_is_a_stub_without_probe`, `test_outage_writes_no_record_state`,
`test_plan_reads_no_store`, `test_reserved_at_fit_not_at_submit`,
`test_unshared_key_never_hits_another_item`, `test_inaccessible_is_none`, `test_initial_build_never_pays_transcribe`,
`test_plan_idempotent_property`.

**Expected-risk existing tests:** none (new module).

**Acceptance:** `mypy --strict`; the table is in the module docstring exactly as implemented.

## Task 5: Domain — card segment, profile rows, unread and coverage for media

**Files:**
- Modify `src/domain/drive_card.py` — `card_text` appends `card_segment(rec)` (Task 3) for a transcribed media record, in
  every file branch it can take (`:161-190`: the profile branch once the transcript is profiled, the brief branch before,
  the machine-text branch when the gate demoted it). No
  `CARD_VERSION` bump (G5): the segment exists only on records with transcript fields, so no existing card's text or hash
  changes (`card_hash` includes `CARD_VERSION`, `drive_card.py:193-194`; a bump would re-embed every card on the drive).
- Modify `src/domain/drive_profile.py` — `PROFILE_POLICY` gains `"audio"` and `"video"` with the subtitles row
  (`KindPolicy(InputRule.CUES, ProfileOutput.FULL, True)`: head 6 000 + tail 1 500, doc2query yes, §27.8 a). No
  `PROFILE_VERSION` bump (§27.13 row "existing PROFILE_VERSION rules").
- Modify `src/domain/drive_search.py` — `_content_kind` / `is_unread` / `is_not_read_yet` treat `Admission.TRANSCRIBE` as a
  content kind (G2); a `no_speech`/`no_audio` stub (`metadata_only / no_text` with `transcript_stub` set) is **not** unread
  (G20); `failed` and `too_large` media are, through the existing `unread` classes (§27.11 "Coverage").
- Modify the port `count_unread` + `FirestoreDriveIndexRepository.unread_count_queries` (`:672-702`) +
  `InMemoryDriveIndexRepository.count_unread` (`:208-216`) — a third kind set `transcribed` (default empty, every caller
  unchanged) whose `metadata_only` classes leave out `no_text` (G20: the Firestore count cannot see `transcript_stub`; every
  media `no_text` is a silent-recording stub, so dropping the reason is exact) and keep `failed`, pending, `too_large`,
  `machine_text`, `user_chunk_cap`. Same filters as today's classes, so **no new composite index**. `drive_access.access_counts`
  (`:32-44`) takes the same sets, so the inaccessible subtraction stays consistent.
- Modify `src/domain/drive_enrich_plan.py` `effective_from` callers (`:203`, `:259-286`) and the planner's `_effective`
  (`drive_index_planner.py:380-389`; `effective_from` itself is `drive_enrich_plan.py:125`) — a `TRANSCRIBED_KINDS` record's `effective_date` never moves to a profile `issued` or
  brief date (§27.8 a: "stays §3.1"; G38: a date said aloud would otherwise re-date the recording).
- Modify `src/services/drive_indexing_service.py` `_unread_counters` (`:633-645`) — the counted kinds become
  `ADMITTED_KINDS | TRANSCRIBED_KINDS` (through the new `transcribed` set) **only when** transcription is on for the user
  (G3); otherwise the call is byte-identical to today's.
- Tests: `tests/unit/domain/test_drive_transcript_card.py`, `test_drive_transcript_unread.py`,
  `tests/unit/services/test_drive_transcript_counters.py`.

**New tests:** `test_card_segment_appended_for_transcribed_media`, `test_untranscribed_media_card_unchanged_hash` (the
pre-existing card hash of a media record is byte-identical), `test_document_card_unchanged_hash`, `test_profile_rows_for_media`,
`test_pending_media_not_read_yet_when_enabled`, `test_pending_media_not_counted_when_disabled`,
`test_no_speech_stub_not_unread`, `test_failed_media_unread`, `test_count_unread_kinds_within_in_limit` (≤ 30 values),
`test_firestore_and_memory_unread_counts_agree_for_media` (parity), `test_no_new_index_for_media_unread_classes`,
`test_spoken_date_never_redates_a_recording` (profile with an `issued` date: effective date unchanged).

**Expected-risk existing tests:** `tests/unit/domain/test_drive_card*.py` only if they enumerate card segments per kind;
`tests/unit/domain/test_drive_profile.py::...::test_no_profile[audio]` and `[video]` (`:61-64`, pins `NO_POLICY` for them —
a certain break, §27.8 a changes it); `tests/unit/domain/test_drive_search.py:132`
(kind assignment table — should stay green). `CARD_VERSION == 4` pins (`test_drive_card_content.py:50`,
`test_drive_capture_card.py:36`) stay green under G5; if the arbiter chooses the bump, they are expected breaks.

**Acceptance:** a property test over random existing records (no transcript fields) shows `card_hash` unchanged.

## Task 6: Billing — batch pricing, the transcribe class, reservations, Cabinet line

**Files:**
- Modify `src/domain/billing.py` — `BATCH_DISCOUNT = 0.5`; `BatchUsage` frozen dataclass (`model`, `audio_input_tokens`,
  `text_input_tokens`, `output_tokens`, `thought_tokens`) with `cost()` = `calculate_cost(model, audio+text, output+thought) ×
  BATCH_DISCOUNT` (one input rate for every modality, thought tokens in the output leg, Δ105/Δ131/Δ132). No new price row:
  `gemini-3.8-flash` stays at the held final price $1.50/$7.50 (`billing.py:172`, `price_consensus.py:101,115`; G7). Waiver
  note: `billing.py` already holds several public classes.
- Modify `src/domain/drive_enrichment.py` — `UsageClass.TRANSCRIBE = "transcribe"`; `ModelUsageMonth` gains
  `transcribe_usd`, `transcribe_initial_usd` (always 0, needed by `cap_usd`'s getattr), `transcribe_reserved_usd`;
  `USD_CAPS[transcribe] = TRANSCRIBE_USD_PER_MONTH`.
- Modify `src/domain/drive_enrich_plan.py` `available_usd` (`:103-105`) — the initial build never pays `transcribe` (G8).
- Modify `src/services/drive_indexing_service.py` `_spend_write` (`:2305-2320`) — `transcribe` dollars never touch
  `spend_initial_build` (G8), and it takes an explicit `month` for them: a batch's real cost is booked in its
  **reservation's** month, at collection time (G9 as revised in Part C), so a month turn never books unreserved dollars
  into the new month's cap.
- Modify the repo port + Firestore + in-memory — `reserve_model_usd(user_id, month, cls, delta_usd)` (server-side
  `Increment` on `<cls>_reserved_usd`; negative releases; the month is the **reservation's** month, G9); `usage_from_doc` reads
  the new fields.
- Modify `src/web/static/cabinet.html` (`:3209`) — the class list gains `"transcribe"`; LANG_DICT key `drive.index_usd_transcribe`
  in en/uk/fr/es.
- Tests: `tests/unit/domain/test_billing_batch.py`, `tests/unit/domain/test_drive_transcribe_usage.py`,
  `tests/unit/adapters/test_drive_model_usage_reservations.py`, `tests/unit/services/test_drive_transcribe_spend.py`.

**New tests:** `test_audio_and_text_input_priced_alike`, `test_batch_discount_on_both_legs`, `test_thought_tokens_in_output_leg`,
`test_unknown_model_batch_cost_is_zero_and_flagged` (mirrors `calculate_cost`), `test_price_rows_hash_pin` (sha256 of the
`gemini-3.8-flash` row and `BATCH_DISCOUNT`: a price edit fails the test and forces a review), `test_transcribe_cap_in_usd_caps`,
`test_initial_build_never_covers_transcribe` (service: open build, a transcribe spend leaves it untouched),
`test_reserve_and_release_same_month`, `test_release_after_month_turn_hits_reservation_month`,
`test_cost_after_month_turn_booked_in_reservation_month`,
`test_concurrent_reservations_never_lost` (in-memory + Firestore fake), `test_cabinet_lists_transcribe`.

**Expected-risk existing tests:** `tests/unit/domain/test_drive_search_v2_frozen_core.py` `:152` (`UsageClass` list),
`:211` (`USD_CAPS` equality), `:236-238` (`ModelUsageMonth` field list) — the spec adds a class; reviewer decides.
`tests/unit/web/test_cabinet_drive_model_dollars.py:15` requires a locale key per class — passes once the key is added (no
edit). `tests/unit/adapters/test_drive_model_usage_initial_share.py`, `test_firestore_drive_index_v2_wire.py` (usage field
lists) if they enumerate fields.

**Acceptance:** at the held price a 30-minute window of 45 000 audio tokens + ~6 000 output (thinking LOW) prices 45 000 × $0.75/M + 6 000 × $3.75/M ≈ $0.056
before thought tokens; the docstring states the ≈ $0.16/h figure (G7).

## Task 7: Repository — jobs, windows, batches, store, queues, purge, sweep, indexes

**Files:**
- Modify `src/config/environment.py` — `drive_transcript_jobs_collection`, `drive_transcript_batches_collection` (G10),
  `drive_transcripts_collection` (env-prefixed, same docstring pattern as `:357-389`).
- Modify `src/ports/drive_index_repository.py`, `src/adapters/firestore_drive_index_repo.py`, `in_memory_drive_index_repo.py`:
  - Jobs `{prefix}drive_transcript_jobs/{user_id}:{item_id}`: `get_transcript_job`, `save_transcript_job`,
    `delete_transcript_job`; window results in the subcollection `windows/{index}` (G11): `save_window_result`,
    `get_window_results`. Fields: `content_key`, `content_version`, `duration_s`, `windows[]` (index, start, length, state
    `extracted|uploaded|submitted|done|failed`, `file_name`, `files_expire_at`, `batch_id`, `attempts`, `reupload_used`),
    `reserved_usd`, `reserved_month`, `transcribed_at`. Never a download URL (§27.4), and never the Gemini file **URI**
    (`https://…/files/<id>`, which `test_no_url_field_ever_written` would also catch): the job keeps the file `name`
    (`files/<id>`) and the adapter resolves the URI when it builds a request (Task 10).
  - Batches `{prefix}drive_transcript_batches/{user_id}:{batch_id}` (G10): `create_batch`, `get_open_batches(user_id, now,
    limit)` ordered by `next_poll_at`, `save_batch`, `delete_batch`. Fields: provider batch name, `state`, `created_at`,
    `next_poll_at`, `request_keys`, `reserved_usd`, `reserved_month`, `kind` (`transcribe` now, `summary` in T3).
  - Store `{prefix}drive_transcripts/{user_id}:{content_key}`: `get_transcript`, `put_transcript` (sharded into `parts/{n}`
    above `TRANSCRIPT_DOC_BYTES`, written parts first, head last), `touch_transcript(last_used_at)`,
    `orphan_transcripts(user_id, before, limit)`, `delete_transcript`.
  - Queues on records: `transcript_queue(user_id, limit)` (`transcript_state == "ready"`, `effective_day` desc, `parent_id`,
    `item_id`, the `enrich_queue` order, `:1006-1012`), `transcript_wakeups(user_id, now, limit)`, `jobs_by_content_key(user_id,
    key)` for the in-flight copy check (G12).
  - Purge helpers: `delete_user_transcript_jobs`, `delete_user_batches`, `delete_user_transcripts` (batched, like
    `delete_user_hints`).
- Modify `config/firestore.indexes.json` — composites: records `user_id + transcript_state + effective_day desc + parent_id +
  item_id`; records `user_id + transcript_wake_at + item_id`; jobs `user_id + content_key`; batches `user_id + next_poll_at`;
  store `user_id + last_used_at`. Heavy fields of the store exempt from single-field indexing (`segments`).
- Modify `scripts/infrastructure/drive_index_smoke.py` — runs the new reads (read-only).
- Tests: `tests/unit/adapters/test_drive_transcript_repo.py` (both adapters via the existing parity style),
  `tests/unit/adapters/test_firestore_drive_transcript_provisioning.py` (the `_required`/`_declared` pattern of
  `test_firestore_drive_index_v2_provisioning.py`).

**New tests:** roundtrip of each document; `test_store_shards_above_doc_bytes_and_reads_back`, `test_shard_head_written_last`,
`test_queue_order_newest_first`, `test_wakeups_due_only`, `test_open_batches_by_next_poll`, `test_jobs_by_content_key`,
`test_orphans_by_last_used`, `test_purge_helpers_delete_only_the_user`, `test_no_url_field_ever_written` (scan every saved
dict for `http` values), `test_every_new_query_has_its_index`, `test_four_hour_transcript_fits_doc_bound` (§27.4 build pin: a
48 000-word synthetic transcript serialises under 1 MiB per part).

**Expected-risk existing tests:** `test_firestore_drive_index_*_provisioning.py` only if they assert the full index list
length; `tests/unit/adapters/test_drive_index_v2_parity.py` if it enumerates port methods.

**Acceptance:** both adapters pass the same suite; every Firestore method keeps the `_store_errors` mapping
(`DriveIndexStoreUnavailable`, `:1273-1290`). A missing or still-building composite index is `FailedPrecondition`, which
is systemic for the whole tick (`_MISSING_INDEX`, `:1269-1270`): every new composite query must be unreachable while the
kill switch or the user's flag is off (asserted in Task 14), and the purge helpers use `user_id ==` only.

**Size:** split for dispatch — **7A** the documents (jobs + `windows/` subcollection, batches, the sharded store; both
adapters, codecs, provisioning test); **7B** queues, wake-ups, `jobs_by_content_key`, orphans, purge helpers,
`firestore.indexes.json`, smoke reads. 7B depends on 7A.

## Task 8: `UserDrivePort.download_url`

**Files:**
- Modify `src/ports/user_drive_port.py` — `download_url(user_id, item_id) -> str` (abstract). **Two** classes subclass the
  ABC, not one: `OneDriveAdapter` and the eval's `FixtureDrive` (`scripts/validation/drive_eval/fixture_drive.py:95`), which
  would no longer instantiate; give `FixtureDrive` an implementation (a stub that raises
  `DriveItemNotFoundError`: the eval never transcribes). Tests use `AsyncMock(spec=UserDrivePort)` and gain the method.
- Modify `src/adapters/microsoft/onedrive_adapter.py` — the GET-item call `download()`/`download_to_file()` already make
  (`:252-258`, `:317-321`), returning `@microsoft.graph.downloadUrl`; missing → `DriveItemNotFoundError`; the URL is never logged.
- Tests: `tests/unit/adapters/test_onedrive_download_url_wire.py` (aiohttp boundary, existing wire style).

**New tests:** `test_returns_download_url`, `test_missing_url_raises_not_found`, `test_404_raises_not_found`,
`test_url_never_logged` (caplog), `test_no_bearer_sent_to_the_url_host` (it is not fetched at all here).

**Expected-risk existing tests:** none (AsyncMock specs gain a method); `tests/unit/scripts/test_drive_eval_*` and
`tests/unit/fixtures/test_drive_eval_fixture.py` must stay green through the `FixtureDrive` method (a failure there is a code
bug).

## Task 9: Media extractor — `MediaAudioPort`, child ops, hardening

**Files:**
- Create `src/ports/media_audio_port.py` — `MediaAudioPort`: `probe(url) -> MediaProbe` (duration from ffprobe, has an audio
  stream, container format), `extract_windows(url, windows, out_dir, *, wall_s) -> Tuple[Tuple[WindowSpec, str], ...]`
  (the windows written before the wall, in order); raises `MediaUnreadable` (per-file, `extract_error`) or returns an outcome
  for `timeout`/`memory`, mirroring `ExtractionStatus` semantics.
- Create `src/adapters/drive_parser/media.py` — child-side: argv builders and runners for ffprobe and ffmpeg. Required flags
  (Δ99): `-nostdin`, `-protocol_whitelist https,tls,tcp,crypto`, a demuxer allowlist (G14: `-format_whitelist
  mov,mp4,m4a,matroska,webm,mp3,wav,flac,ogg,aac` on both tools, plus the probed format pinned with `-f` on the extraction
  run), `-map 0:a:0`, `-vn -sn -dn`, no filter graph from the file, output `-ac 1 -ar 16000 -c:a libopus -b:a 24k` to
  `window_<k>.opus` in the child's own directory. One window per `-ss <start> -t <len>` (input-side) by default; the `segment`
  muxer in one pass when all remaining windows of the file fit the step (Δ134). Also pinned: `-hide_banner -loglevel error
  -nostats` (bounds the stderr file the digest reads), and **TLS verification on** — ffmpeg does not verify a server
  certificate by default (`tls_verify` defaults to 0), so the argv carries `-tls_verify 1 -ca_file <system CA bundle>` for the
  Graph URL (the builder takes the CA path as a parameter; not an RFC flag, a hardening addition of the same Δ99 kind).
  **A probe without a duration** (a browser-recorded webm/mkv has no `Duration` and no `Cues`; ffprobe says `N/A`, and `-ss`
  cannot seek) takes the one-pass `segment` path from the start, `-t TRANSCRIBE_MAX_SECONDS` bounding the pass, the duration
  read from the produced windows; a pass that reaches the bound is the `too_long` stub (G35).
- Modify `src/adapters/drive_parser/child.py` `dispatch` — ops `media_probe`, `media_windows`, loaded after `serve` applied the
  limits. The URL arrives in the stdin request (≤ 64 KiB, `:35`), never in the indexer's own argv and never logged; it is in
  the ffmpeg grandchild's argv (`-i <url>`), readable only by root and the `driveparser` uid — the named Δ99 secret.
- Create `src/adapters/drive_parser/media_launch.py` and `media_extractor.py` (`SubprocessMediaExtractor(MediaAudioPort)`):
  same launcher contract as `SubprocessContentExtractor` (fresh child per op, `apply_limits` before any import, allowlisted
  env, private workdir owned by `DRIVE_PARSER_USER`, process-group kill on the wall, `memory_scope(SCOPE_SUBPROCESS,
  "drive_media")`, stderr digest only), its own limits (`RLIMIT_CPU` 150 s, wall `TRANSCRIBE_EXTRACT_WALL_S`, `RLIMIT_FSIZE`
  64 MiB, `RLIMIT_AS` 1 GiB), one child at a time (shares the instance with the parser; the tick runs one stage at a time).
  Collected windows: regular files only, `O_NOFOLLOW`, size-checked (the `_load_result`/`_collect_window` rules,
  `subprocess_content_extractor.py:336-376`). The launcher is **duplicated, not shared** with `SubprocessContentExtractor`
  (G15).
- Tests: `tests/unit/adapters/test_drive_media_argv.py`, `test_drive_media_extractor.py`, `test_drive_media_hostile.py`.

**New tests:**
- Argv pin: `test_protocol_whitelist_pinned`, `test_format_whitelist_pinned`, `test_nostdin_and_no_filters`,
  `test_audio_map_only`, `test_input_side_seek`, `test_url_not_in_parent_logs`.
- Launcher: `test_env_is_allowlist`, `test_runs_as_parser_user_when_configured` (patched `pwd`, the existing sandbox-test
  pattern), `test_wall_kills_group`, `test_collected_window_must_be_regular_file`.
- `test_tls_verify_on_with_ca_file`, `test_probe_without_duration_takes_one_pass_segment` (a cue-less webm written by
  ffmpeg to a pipe has no duration), `test_one_pass_bounded_by_max_seconds`.
- Hostile set under the rlimits (real ffmpeg; `skipif` no ffmpeg). The server is a local **HTTPS** server with a test CA the
  argv's `ca_file` names — a plain `http.server` would be refused by `-protocol_whitelist https,…` for every file, good and
  hostile alike, and widening the whitelist for the test would test something else: an
  `.m3u8` playlist pointing at `file:///etc/passwd` and at another host (refused by the whitelists), a `concat` list, a
  zero-length file, a truncated mp4 (no `moov`), a 1×1 video with 4 h of silence generated by ffmpeg (probe returns 4 h, the
  extraction of one window stays under the limits and yields a valid Opus file). Each ends as a defined outcome, never a hang.
- `test_image_ffmpeg_supports_flags` — `pytest.mark.docker`: in the built image, `ffmpeg -h full` lists `format_whitelist` and
  `protocol_whitelist`, `tls_verify` is honoured (a self-signed server without its CA is refused), `libopus` is present, and
  the hostile set passes as the `driveparser` user. (The base image is `python:3.11-slim`; the test checks the Debian
  release it actually ships rather than assuming bookworm.)

**Expected-risk existing tests:** `tests/unit/adapters/test_drive_parser_child_dispatch.py` if it asserts the full op set
(`unknown op` path). `tests/unit/test_req_arch_01_hexagonal_isolation.py` must stay green (the new adapter lives in
`drive_parser/`, imports only intra-subpackage, domain and ports).

**Acceptance:** the indexer process never decodes a byte of media; every ffmpeg/ffprobe run is a child under the limits.

## Task 10: `BatchTranscriptionPort` + `GeminiBatchAdapter`

**Files:**
- Create `src/ports/batch_transcription_port.py` — `upload_audio(path, mime) -> FileRef` (`name`, `uri`, `expires_at`),
  `submit(requests: Sequence[BatchRequest], *, display_name) -> BatchRef` (a request's file is referenced by `name`; the
  adapter turns it into the `file_uri` itself, Task 7), `poll(batch) -> BatchStatus` (`pending|running|
  succeeded|failed|cancelled|expired`), `collect(batch) -> Mapping[str, BatchResult]` (per key: `text` or `error`, `usage`
  as `BatchUsage`, `finish_reason`), `delete_file(ref)`. `BatchRequest`: key, system instruction, user text, optional file ref,
  response schema, thinking level, max output tokens. Generic enough for T3's text-only summary requests.
- Create `src/adapters/gemini_batch_adapter.py` — over `google-genai` (T0: 2.17.0): `files.upload`, `batches.create` with
  **inline** requests carrying `response_schema` (typed `types.Schema`, never `response_json_schema`: silently ignored in
  batch, Δ131) **together with `response_mime_type="application/json"`** (Gemini needs both; T0's requests carried both),
  `thinking_config=ThinkingConfig(thinking_level=LOW)` on every request, the key in `InlinedRequest.metadata` and checked
  on the way back in `InlinedResponse.metadata` (both fields exist in 2.17.0; never trust order alone), `batches.get`,
  per-response error check (a `SUCCEEDED` job holding a code-3 error
  in one response is that window's failed attempt), usage from `prompt_tokens_details` (AUDIO/TEXT) + `candidates_token_count`
  + `thoughts_token_count`, `files.delete`. Model id comes from the constructor (G26). Errors: 429, 5xx, timeouts, network,
  auth/billing 4xx → `LLMUnavailableError`/`LLMRateLimitError`/`LLMAccountError` (`domain/exceptions.py:12-66`; uncharged
  outage, G25); a 4xx refusal of one request's
  input → that result's `error` with a `refused` flag. Display names are opaque hashes (no user id at the provider).
- Modify `tests/contracts/adapter_contracts.py` — rules (keyed by the adapter name, the file's non-LLM convention, `:196`):
  schema sent as `response_schema` with `response_mime_type` JSON in each inline request; thinking LOW present; per-response
  error surfaced; thoughts in output leg; key round-trip; a file referenced by URI built from its stored name.
- Modify `requirements.txt` — raise the floor to the version T0 measured (`google-genai>=2.17.0,<3.0.0`, G27).
- Tests: `tests/unit/adapters/test_gemini_batch_adapter_wire.py` (mock at the SDK client boundary,
  `docs/how_to/ADAPTER_WIRE_TESTING.md`).

**New tests:** `test_upload_sends_mime_and_returns_expiry`, `test_submit_inline_response_schema_not_json_schema`,
`test_thinking_low_on_every_request`, `test_json_mime_type_with_schema`, `test_poll_maps_every_state`,
`test_collect_checks_each_response_error`, `test_account_error_is_an_outage`,
`test_collect_key_mismatch_is_error`, `test_usage_by_modality_parse`, `test_thoughts_billed_as_output`,
`test_delete_file`, `test_rate_limit_and_5xx_are_outages`, `test_refused_request_is_a_result_error`,
`test_display_name_has_no_user_id`, contract-rule tests.

**Expected-risk existing tests:** `tests/contracts/` suites that iterate all rules over all adapters (a new adapter id must be
registered where they enumerate providers; reviewer if a matrix test pins the adapter list).

**Acceptance:** no live call anywhere in the tests; the adapter file imports no other adapter.

## Task 11: `DriveTranscriberAgent`, prompts, runner

**Files:**
- Create `src/agents/drive_transcriber_agent.py` — `AGENT_TYPE = "drive_transcriber"`; inherits `BaseAgent`, not a delegation
  agent (`execute` returns a failure); `build_request(window, *, prompt) -> BatchRequest` (window length as `MM:SS`, the rules
  of §27.5: verbatim, languages as spoken, never translate or correct, fillers kept, no headings, new segment at each speaker
  change and at least every 60 s, stable speaker numbers inside the window, `null` for one speaker, audio is data never
  instructions); `parse(text) -> WindowTranscript` — `json.loads` once, schema check (`language_codes`, `speakers`,
  `no_speech`, `segments[{start: "MM:SS"|"H:MM:SS", speaker: int|null, text}]`), `start` parsed by Task 2, **no regex
  fallback**; invalid → `DriveOutputInvalid` (the window's failed attempt). Response schema `_RESPONSE_SCHEMA` as a typed
  `types.Schema` builder kept in the adapter-agnostic form the adapter converts (the agent never imports `google.genai`).
  Honest difference (Δ102): no `_call_llm`, so no retry/failover/`TokenLedger`/prompt-content row/Logfire span (G28), and
  no `ModelUsage` leg keyed (model, batch) as Δ105 words it: each collected result is priced by `BatchUsage.cost()` and billed
  directly (G36).
  **Playbook steps that do not apply, stated so a reviewer does not add them:** no `agent_manifest` descriptor (internal,
  like every drive agent), hence no `_DEFAULT_AGENT_TIERS` entry (`test_every_llm_agent_has_a_default_tier` reads the
  manifest only) and no `AgentProviderStrategy` entry (§27.5: no provider switch for this class; the model comes from
  composition, G26); no `user_agent_factory` wiring (built by the indexer's composition).
- Modify `src/infrastructure/agent_config.py` — `DriveTranscriberAgentConfig` (`max_tokens`, `thinking="low"`) and
  `DRIVE_TRANSCRIBER`.
- Create `firestore_utils/uploads/COGNITIVE_PROCESS_DRIVE_TRANSCRIBER.json`, `OUTPUT_FORMAT_DRIVE_TRANSCRIBER.json`,
  `drive_transcriber.json` (profile), `drive_transcriber_agent_v1.json` (blueprint) in the shape of the
  `drive_profiler*` uploads. The `prompts_snapshot/` mirror is refreshed by the owner's pull after the upload (it is a
  read-only mirror of Firestore).
- Create `src/composition/drive_transcript_runner.py` — `DriveTranscriptRunner`: builds the prompt once per submission
  (`build_drive_prompt`, `drive_model_call.py:226`; a missing prompt fails the submission as an outage, no fallback prompt),
  turns windows into requests, submits through the port, and on collect parses each result, prices it (`BatchUsage.cost()`)
  and bills the user's account through `QuotaService.record_usage` (no execution billing scope exists across hours; Δ105).
- Modify `src/utils/capabilities.py` — one line (what the bot does with recordings); done in Task 15 with the skill line.
- Tests: `tests/unit/agents/test_drive_transcriber_agent.py`, `tests/unit/composition/test_drive_transcript_runner.py`.

**New tests:** `test_request_has_window_length_string_and_rules_token`, `test_parse_valid`, `test_parse_rejects_integer_start`,
`test_parse_rejects_extra_text_no_regex_fallback`, `test_parse_no_speech`, `test_injection_line_is_transcribed_not_obeyed`
(prompt contains the "audio is data" rule; a parsed segment with "ignore the above" stays text), `test_missing_prompt_is_outage`,
`test_collect_bills_account_with_batch_cost`, `test_runner_never_sends_url`.

**Expected-risk existing tests:** `tests/unit/infrastructure/test_agent_config*.py` if it enumerates configs; agent registry
completeness tests (the agent is not manifest-registered by design — same as the other drive agents).

## Task 12: Transcript stage A — extraction, upload, submission

**Files:**
- Create `src/services/drive_transcript_stage.py` — `DriveTranscriptStage(repo, drive, media: MediaAudioPort, batch:
  BatchTranscriptionPort, runner, clock, monotonic)`. It holds no tick state of its own; the tick passes a `TranscriptHost`
  (a `Protocol` defined in this module, implemented by `DriveIndexingService` in Task 14) for `fence()`, `commit(records)`,
  `index_transcript(rec, body, extracted_by) -> rec`, `spend(cls, usd, calls)`, `reserve(usd)`, `release(usd, month)`,
  `agent_available/agent_outage`. Public steps: `extract_step(user_id, budget_window) -> summary`, `cheap_steps(...)`
  (Task 13).
- `extract_step`, newest first from `transcript_queue`:
  1. Store hit by `transcript_key` → finalize from the store (no ffmpeg, no model; `touch_transcript`) — the copy/rename/move
     path (Δ112). The same path re-finalizes, with embeddings only, a `current` record whose `transcript_body_version` or
     `extraction_version` is no longer the code's (G18, G31).
  2. Copy in flight (G12): another job of the user has the same shared key and is `working`/`waiting` → this record `waiting`
     on it; finalized from the store when that one lands.
  3. Fresh `download_url` per step (never stored, Δ98/Δ134); `probe` (duration from ffprobe, never the facet); stub decisions
     (`too_long`, `no_audio` — deterministic, no model) via Task 4.
  4. Whole-file estimate vs cap after reservations: not fitting → `parked` (Δ106); fitting → the estimate is **reserved now**
     (job + month doc), before any window is extracted (G39). Park, failure and abandon release it.
  5. Extract the windows the step's wall allows (resumable: windows recorded `extracted`/`uploaded` are skipped), upload each
     as it completes (`files_expire_at` recorded), record after the provider answered.
  6. When every window of the file is uploaded: mark windows `submitted` in one batch per user per submission (all ready
     windows of the user's files, keys `{item_id}:{window}`, Δ103), create the batch document carrying the reservations of
     the files it holds.
- Outage: `drive_transcriber` backoff (`model_backoff_by_agent`, §25.4.6) stops extraction and submission; cards, enrichment
  and everything else go on. A file ffmpeg cannot read is a per-file attempt (`extract_error`), `failed` at `MAX_ATTEMPTS`,
  no stub. Edit cooldown and quiet period via Task 4.
- Tests: `tests/unit/services/test_drive_transcript_stage_extract.py` with a fake `MediaAudioPort` and a fake
  `BatchTranscriptionPort` (new `tests/unit/services/drive_transcript_fakes.py`).

**New tests:** `test_cache_hit_finalizes_with_no_ffmpeg_and_no_model`, `test_copy_shares_one_transcription` (two records, one
key, one extraction, one submission, both `current`), `test_resume_after_killed_tick_skips_uploaded_windows`,
`test_fresh_download_url_per_step`, `test_url_never_persisted`, `test_probe_duration_beats_facet`, `test_too_long_stub_no_model`,
`test_no_audio_stub_no_model`, `test_unreadable_file_fails_at_max_attempts_without_stub`, `test_cap_parks_whole_file`,
`test_reservation_written_before_first_extraction`, `test_two_files_never_both_pass_one_cap`,
`test_one_batch_per_user_per_submission`, `test_outage_stops_extraction_only`, `test_edit_cooldown_holds`,
`test_wall_bounds_one_step`, `test_body_or_extraction_version_mismatch_refinalizes_without_model`.

**Expected-risk existing tests:** none (new service).

## Task 13: Transcript stage B — poll, collect, settle, resubmit, finalize

**Files:**
- Modify `src/services/drive_transcript_stage.py` — `cheap_steps(user_id, budget)`: (1) poll at most `BATCH_POLLS_PER_TICK`
  open batches whose `next_poll_at ≤ now` (first `BATCH_POLL_FIRST_S`, then `BATCH_POLL_EVERY_S`, `BATCH_POLL_SLOW_S` after
  `BATCH_SLOW_AFTER_S`; one poll per batch, Δ104/Δ133); (2) collect a succeeded batch: per window parse and checks, each
  with the RFC's own retry rule (G37): invalid JSON, a request-level error or a short window by the coverage check (Δ129) →
  the window's attempt, resubmitted in the next submission until `MAX_ATTEMPTS`, then a gap marker for the span (never
  stitched short); the time validator (§27.6) → **one** resubmit, then the gap marker; an interpolation share above
  `INTERPOLATED_MAX_SHARE` → **one** resubmit, then accepted with its `~` stamps; a refused window → gap marker, transcript
  `truncated`. A result whose record is gone, or whose job is at another content version, is settled (cost booked,
  reservation released, Gemini file deleted) and dropped, and the job deleted (G33). Settle: release the batch's reservation
  in its month, book the real cost (`spend("transcribe", …)`, the reservation's month, G9), bill the
  account; delete the window's Gemini file on success and on abandon; (3) a batch `failed`/`expired`/`cancelled`: release,
  resubmit its open windows once (re-extract and re-upload a window whose file passed `files_expire_at`, once per window,
  Δ104); (4) finalize a file whose windows are all `done`: stitch (Task 3), write the store (sharded), render the body, hand it
  to `host.index_transcript` with `extracted_by = "transcribe-gemini@<TRANSCRIBE_VERSION>"`, set `transcript_state=current`,
  `transcript_seconds/speakers/languages`, `transcribed_at`; a `no_speech` result → record `metadata_only / no_text`, stub
  `no_speech`. A finalize embeds up to ≈ 125 chunks (4 h ≈ 300 000 chars / `CHUNK_CHARS` 2 400) under
  `EMBED_FILE_BUDGET_S` 180 s, so it is not "cheap": each one starts only while `budget.time_left()` and takes one of the
  tick's `TICK_FILE_BUDGET` files, like an index run (G32). Finalize is idempotent: a job whose windows are all `done` and
  whose record is not `current` is finalized again (a crash mid-finalize leaves only swept chunk debris, §7.5).
- Orphan sweep: once per day per user (sync-state-free: by `orphan_transcripts(before=now − TRANSCRIPT_ORPHAN_TTL)`), delete
  store entries no record referenced (`last_used_at`), §27.8 b. Gated by both switches like every other stage query.
- Tests: `tests/unit/services/test_drive_transcript_stage_collect.py`.

**New tests:** `test_first_poll_after_150s_then_300s`, `test_slow_cadence_after_one_hour`, `test_at_most_10_polls_per_tick`,
`test_batch_waiting_for_hours_resumes_across_ticks` (clock advanced 6 h over many ticks; T1 check of what T0 did not test),
`test_job_expiry_resubmits_open_windows_once`, `test_expired_file_reuploaded_once` (window file older than 48 h),
`test_succeeded_job_with_error_response_is_failed_attempt`, `test_short_window_is_resubmitted_not_stitched`,
`test_gap_marker_after_max_attempts`, `test_refused_window_gap_marker_truncated`, `test_settle_releases_and_books_real_cost`,
`test_failed_batch_releases_reservation`, `test_files_deleted_on_success_and_abandon`, `test_no_speech_has_no_chunks`,
`test_finalize_through_host_fence` (a changed content version during the run drops the result, §7.4),
`test_orphan_sweep_after_ttl`, `test_time_validator_resubmits_once_then_gap`, `test_interpolation_share_resubmits_once_then_accepts`,
`test_result_for_deleted_record_settled_and_dropped`, `test_crash_during_finalize_refinalizes`,
`test_finalize_takes_a_file_budget_slot`.

**Expected-risk existing tests:** none.

## Task 14: Tick integration, composition, kill switch, purge, tick line

**Files:**
- Modify `src/services/drive_indexing_service.py`:
  - Constructor: `transcripts: Optional["DriveTranscriptStage"] = None` (`TYPE_CHECKING` import only, REQ-ARCH-22),
    `transcribe_enabled: bool = False` (the kill switch). Per-user flag read with the opt-in flag in `_enabled`
    (`:3098-3103`), cached per tick.
  - Implements `TranscriptHost`; `index_transcript` reuses the ordinary text path: `generation_started`, `_read_text`
    (`:1650-1713`: gate, chunker, chunk cap, embeddings, the §7.4 fence), `_end_run`; then the existing enrichment jobs profile
    and hint the transcript.
  - `_commit` runs `transcript_plan` next to `enrich.planned`.
  - Placement (§27.3): `cheap_steps` first in `_work_off` (after the sweep, before `_due_loop`), taking no share (finalizes
    take file-budget slots, G32); the extraction step in `_work_off` right after `_due_loop` returns (the enrichment phase ran
    inside it, `:953-998`), **whatever `stopped_class` it returns** and before `_upgrade_step` (which runs only when
    `stopped_class is None`, `:867-870`), with a start window of at least `TRANSCRIBE_SHARE` of the start budget, only while the
    worst case `TRANSCRIBE_EXTRACT_WALL_S + TRANSCRIBE_UPLOAD_S + FILE_START_MARGIN_S` ends before `TICK_DEADLINE_S`; content
    and enrichment yield `TRANSCRIBE_SHARE` only while the user has ready extraction work (G16).
  - `_has_more`: ready extraction work for an enabled user continues the chain; a `transcript_wake_at` in the future or
    open batches alone do not (the 10-minute fan-out polls, Δ104).
  - `_purge` (`:2981-3007`): before the records, delete the user's jobs, batches (best-effort `delete_file` of their Gemini
    files; failures logged, files expire in 48 h, G30) and — unless `purge_keep_transcripts` (read at purge start) — the store;
    the user's open reservations are released explicitly in their own months (the usage documents survive purges by design,
    §25.4.5; G9).
  - `_log` tick line: `windows_extracted`, `windows_submitted`, `windows_collected`, `transcripts`, `transcribe_usd`,
    `transcribe_reserved_usd`.
- Modify `src/composition/drive_model_setup.py` — `build_drive_transcription(...)`: `GeminiBatchAdapter(api_key, model=
  GeminiAdapter.MODEL_TIERS[PerformanceTier.BALANCED])` (G26; never flash-lite, D44), the runner, the media extractor with
  `DRIVE_PARSER_USER`. **Built only when the kill switch is on** (with it off the indexer's object graph is today's, so a
  deploy OFF changes no wiring); without `GEMINI_API_KEY` it is not built (logged ERROR when the kill switch is on). The purge
  helpers need only the repository and run either way; a purge with the stage unbuilt skips the best-effort Gemini
  `delete_file` (G30: the files expire in 48 h).
- Modify `indexer_main.py` — read `DRIVE_TRANSCRIBE_ENABLED` with `os.getenv` (`on` only for `1/on/true/yes`, default off), pass
  it and the stage; docstring env list gains the knob. If the arbiter accepts G34, also `DRIVE_TRANSCRIBE_USD_CAP` (optional
  `os.getenv` knob, a positive float that **lowers** `TRANSCRIBE_USD_PER_MONTH` for the tick's cap math; never raises it;
  unset = the parameter), its own deploy-only key `DRIVE_TRANSCRIBE_USD_CAP_DEV` and substitution, like the switch.
- Modify `cloudbuild-dev.yaml` (indexer `--set-env-vars` `:141`, the `:116-118` comment, **and the `substitutions:` defaults
  block `:290-292`** — `_DRIVE_TRANSCRIBE_ENABLED: "off"`, so a manual `gcloud builds submit` without the key deploys OFF, not
  an empty value) and `Makefile` (`:74-78`, `:283`): `_DRIVE_TRANSCRIBE_ENABLED` from `DRIVE_TRANSCRIBE_ENABLED_DEV`, default
  `off` (G13, the `DRIVE_UPGRADE_PASS` pattern). The RFC's §27.15 step 4 wording ("not a Cloud Build substitution") is
  corrected in Task 17.
- Tests: `tests/unit/services/test_drive_indexing_transcripts.py`, `tests/unit/test_indexer_main_transcribe.py`,
  `tests/unit/test_cloudbuild_transcribe_knob.py`.

**New tests:** `test_kill_switch_off_no_stage_runs` (no ffmpeg, no upload, no poll, no model), `test_user_flag_off_no_stage_runs`,
`test_both_on_runs_stage`, `test_deadline_sum_assertion` (the `enrichment_allowed` precedent `:255-260`),
`test_share_left_for_extraction_after_enrichment`, `test_shares_unchanged_when_no_transcript_work`,
`test_no_continuation_while_only_waiting`, `test_continuation_while_extraction_ready`, `test_finalized_transcript_is_searchable`
(chunks readable, card segment, profile/passage jobs `ready`), `test_purge_deletes_jobs_batches_and_store`,
`test_rebuild_purge_keeps_store`, `test_tick_line_counters`, `test_knob_default_off_in_cloudbuild_and_makefile`,
`test_substitution_default_off`, `test_indexer_wires_stage_with_balanced_model`, `test_switch_off_builds_no_stage`,
`test_no_new_composite_query_while_off` (a repo spy: with either gate off, a full tick sequence — upgrade pass over media,
purge, counters — calls none of `transcript_queue`, `transcript_wakeups`, `get_open_batches`, `jobs_by_content_key`,
`orphan_transcripts`, `get_transcript`), `test_extraction_runs_when_due_loop_stopped_at_a_class`, (G34 if accepted)
`test_cap_override_only_lowers`.

**Expected-risk existing tests:** `tests/unit/test_indexer_main.py` (wiring smoke of `build_app()`), `tests/unit/test_cloudbuild_*`
(step/env assertions on the indexer step, e.g. `test_cloudbuild_drive_upgrade_pass.py`; `test_cloudbuild_arg_length.py` must
stay green — the env arg grows by one pair), `tests/unit/services/test_drive_indexing_content_deadline.py`
and `test_drive_indexing_enrichment_tick.py` **only if** G16's yield fires without transcript work (it must not; a failure there
is a code bug).

**Acceptance:** with the knob off, the whole existing suite is green unchanged; with both gates on, a fake-backed end-to-end
tick sequence goes `ready → working → waiting → current` and the file is found by what was said.

**Size:** split for dispatch — **14A** host protocol, `_commit` plan, placement and shares, `_has_more`, tick line (service
only, fakes); **14B** purge, composition, `indexer_main`, Cloud Build/Makefile knob(s). 14B depends on 14A.

## Task 15: Read side — open, `original`, skill, capabilities, disclosure

**Files:**
- Modify `src/services/drive_search_service.py` — `transcript_text(user_id, item_id) -> Optional[str]`: the record (must be
  `current`, not a stub), the store by `transcript_key`, `open_text` (Task 3) inside `wrap_untrusted` with the reference reminder
  (D2, §10.4); capped (G19).
- Modify `src/agents/file_management_agent.py` `_open_drive_item` — a transcribed audio/video ref returns the transcript (never
  the bytes); `original: true` returns the drive link (`web_url`), never bytes into chat, audio and video alike; a recording
  with no transcript keeps today's branch (Δ121).
- Modify `src/infrastructure/agent_manifest.py` — `context_schemas[OPEN_FILE_FROM_DRIVE]` (`:634`) gains `original`, a
  JSON-schema dict `{"type": "boolean", "description": "open_file_from_drive only: set true only when the user asks for the
  recording itself, not its content"}` (the declaration builder passes a dict through, `base_agent.py:795-801`; every intent's
  fields merge into **one** `delegate_to_specialist.context` and the first description per name wins, `:796`, hence the
  intent prefix the search fields already use, `:644-645`). The capability description (`:574-577`) names it in its
  `context={…}` block, so `test_manifest_prose_matches_tool_schema.py` stays green (the strict-grammar lesson of
  `project_whitespace_runaway`: declared keys only). Note for deploy OFF: Smart's tool schema changes for every user at
  deploy (one prompt-cache miss); with no transcript, `original` only changes an audio/video open from bytes to a link.
- Modify `src/skills/smart/drive-search/SKILL.md` — the one line of §27.11 (the Transcripts folder clause lands with T2,
  Task 21). Modify `src/utils/capabilities.py` — one line.
- Modify `src/web/user_cabinet_app.py` drive status — returns `transcribe_enabled`; `cabinet.html` shows the §27.12 disclosure
  sentence (LANG_DICT en/uk/fr/es) **only** when it is true; no toggle (G24).
- Tests: `tests/unit/services/test_drive_search_transcript_text.py`, `tests/unit/agents/test_file_management_open_transcript.py`,
  `tests/unit/web/test_cabinet_transcribe_disclosure.py`.

**New tests:** `test_open_transcribed_audio_returns_wrapped_transcript`, `test_open_transcribed_video_never_sends_bytes`,
`test_original_true_returns_link_only`, `test_untranscribed_video_keeps_file_branch`, `test_stub_open_says_none`,
`test_transcript_text_capped_with_note`, `test_wrapper_neutralises_lookalike_tags`, `test_manifest_original_param_described`,
`test_disclosure_only_when_enabled`.

**Expected-risk existing tests:** `tests/unit/agents/test_file_management_agent*.py` cases opening a video (should stay green:
no transcript fields); `tests/unit/infrastructure/test_agent_manifest_drive.py` and any snapshot that pins the open intent's
payload keys or the delegate tool's property set (reviewer); `test_manifest_prose_matches_tool_schema.py` (must stay green);
skill snapshot tests if any pin `SKILL.md` text.

## Task 16: Scripts — flag, backlog, requeue, sweep, rebuild keep, fixtures and eval

**Files (every script: read-only or dry-run by default; `--apply` human-only; PII output only to `scripts/memory/`):**
- `scripts/infrastructure/drive_transcribe_flag.py` — sets `drive_transcribe_enabled` for one user (dry-run default; the owner
  runs `--apply`, G29).
- `scripts/infrastructure/drive_transcribe_backlog.py` — §27.15 step 5: sums `facets.video.duration`, counts audio by bytes at
  10.8 MB/h, prints minutes and cost at $0.08/$0.16/$0.40 per hour (and the held-price figure, G7).
- `scripts/infrastructure/drive_transcribe_requeue.py` — Δ122: lists what a `TRANSCRIBE_VERSION` bump would redo and its
  estimate; `--apply` resets those jobs.
- `scripts/infrastructure/drive_transcript_sweep.py` — §27.13/§27.15 step 7: after a rollback to pre-§27 code, removes the
  three collections' orphan documents (T2 adds the mirror collection) and zeroes `transcribe_reserved_usd` in the usage
  documents of the months its jobs and batches reserved in (otherwise those reservations outlive their documents).
- Modify `scripts/infrastructure/drive_index_rebuild.py` — sets `purge_keep_transcripts` before the purge.
- `scripts/validation/drive_transcript_fixtures.py` — §27.14 Eval, in the style of `drive_capture_fixtures.py`: scripted
  dialogue rendered by local TTS (`espeak-ng`/`say`, two voices; en, es, uk, ru), muxed with a black video track for the video
  family, a tone-and-silence and a music-only clip for `no_speech`, a 70-minute two-window clip for seams, **unique** audio
  over 22 minutes (the T0 gap), short clips for the early-timestamp effect; writes media to `scripts/memory/` and commits
  only the script and the gold JSON (each line's text and intended start).
- `scripts/validation/drive_eval/transcript_eval.py` — owner-run live: WER vs script, speaker-turn agreement, timestamp
  error at probes, seam duplicates/losses, speaker over-count, interpolation share, coverage-check resubmits; Ukrainian
  reported separately; bars written to `bars_transcripts.json` from the first live run (§24.11 practice), never invented.
  Prints a cost estimate and requires `--yes` (memory `feedback_no_live_runs_without_ok`).
- Tests: `tests/unit/scripts/test_drive_transcribe_scripts.py`, `test_drive_transcript_fixtures.py` (TTS and ffmpeg stubbed),
  `test_drive_transcript_eval_metrics.py` (metrics on synthetic pairs, no network).

**Expected-risk existing tests:** `tests/unit/scripts/test_drive_index_rebuild_*.py` (a new flag write before the purge; the
reviewer decides if a call-sequence assertion breaks).

## Task 17: T1 documentation

**Files:** `docs/05_building_blocks/user_drive/README.md` (§0 index row, §1 overview, §2.1 ports/adapters, §3 kinds table,
a new §4.23 "Transcripts" — job model, budgets, cache, privacy, outcomes — §4.11/§4.13 budgets and cost, §5 tick order,
§7 operations: owner steps, kill switch, rollback; memory `feedback_drive_doc_always_current`); RFC §27 header status line
("T1 built on `feat/drive-transcripts`, not deployed") and the resolved G-items as new Δ rows (Δ136…); a decision record
`docs/04_solution_strategy/decisions/drive_transcripts_t1.md` superseding `drive_audio_video_deferred.md` (both linked);
root `CLAUDE.md` (agent roster rows `DriveTranscriber`, User Drive indexing paragraph: transcripts, the knob, the flag);
`src/agents/CLAUDE.md` roster; `docs/07_deployment/README.md` (indexes, prompt upload, knob, morning enable).

**Acceptance:** docs say what the code does, including every G-resolution; no PII.

---

# Part B — T2: the mirror folder, and T3: summary and header polish

## Task 18: Port conflict mode, mirror-root classification, sync-state mirror fields, localization on the indexer

**Files:**
- `UserDrivePort.upload(..., conflict: Literal["rename","replace","fail"] = "rename")`; `OneDriveAdapter` uses it in both the
  simple PUT and the upload session (`onedrive_adapter.py:267-277`); default keeps every caller (Δ115). The eval's
  `FixtureDrive.upload` (`scripts/validation/drive_eval/fixture_drive.py:95`) takes the parameter too.
- `DriveSyncState` gains `mirror_root_id: str = ""`, `mirror_root_name: str = ""`, `mirror_audit_at`, `mirror_scan_cursor`.
  `mirror_root_id`/`mirror_root_name` are written through a dedicated fenced repo method `set_mirror_root(user_id, id, name,
  owner)` the moment Graph answers the folder creation (the §27.9 orphan window shrinks to that one call), the tick's
  in-memory state updated with it, and they are **excluded from `save_sync_state`'s field list** like the fan-out flags, so a
  state snapshot taken before the root existed (a lease-held script save, a tick that read the state earlier) cannot write it
  back to empty (G23 as corrected in Part C). The audit/scan fields stay ordinary tick-owned state fields.
- `PlanContext` gains `mirror_root_id`; `classify` returns `Scope.INSIDE_OPAQUE / metadata_only / folder` for that folder id,
  so its files get no record, no card, no content work and no duplicate hit (Δ116, existing enum value); the bot's own writes
  echoing back in delta fall under the same rule.
- `UIMessage` + `src/locales/{en,uk,fr,es}.py`: `DRIVE_TRANSCRIPTS_FOLDER`, the `(Alek)` suffix, `Long paths`, the README line,
  the header labels (source, duration, speakers, languages, "managed folder"), the four stub sentences, the gap marker sentence
  for the mirror. The indexer gets `LocalizationService` + the language resolution wired (the deep-research copy precedent,
  `deep_research_drive_copy_service.py:73-83`).
- Tests: `tests/unit/adapters/test_onedrive_upload_conflict_wire.py`, `tests/unit/domain/test_drive_mirror_root_scope.py`,
  `tests/unit/adapters/test_drive_mirror_root_state.py`, `tests/unit/locales/test_drive_transcript_strings.py`.

**New tests:** `test_default_conflict_is_rename`, `test_replace_in_simple_and_session_upload`, `test_mirror_root_inside_opaque`,
`test_files_under_mirror_get_no_record`, `test_mirror_subfolder_records_have_no_card`, `test_end_of_tick_save_keeps_mirror_root`,
`test_set_mirror_root_fenced`, `test_every_locale_has_every_key`.

**Expected-risk existing tests:** `test_firestore_drive_index_repo_serialization.py` sync-state field-set tests (`:154-163`,
a new excluded field set); OneDrive upload wire tests asserting the exact URL (default unchanged — should stay green);
locale completeness tests (pass once all four locales carry the keys).

## Task 19: Mirror domain — names, paths, desired vs actual, file render

**Files:** `src/domain/drive_transcript_mirror.py` (pure): `mirror_name(source_name)` (`<name>.md`; over `MIRROR_NAME_MAX` cut +
`~<8 hex of the full name's hash>`, Δ118); `mirror_path_ok(parts)` (over `MIRROR_PATH_MAX` → `<root>/Long paths/<hash>.md`);
`desired(records, folders)` (every record with a `current` transcript, stubs included, plus the source folders above them —
ancestors are `normal`, §27.9); `diff(desired, actual) -> actions` (create, replace, rename/move, delete, prune empty folders
bottom-up, never the root; newest transcript first; a source-folder rename is **one** move of one mirror folder, Δ113);
`render_file(store, rec, strings, *, render_version)` (header naming the source with its `web_url` link, duration, speakers,
languages, "managed folder" line; the body of Task 3; stub files as one localized sentence with the duration) and
`rendered_hash` including `RENDER_VERSION` (Δ119). `RENDER_VERSION = 1` lives here (G6).
**New tests:** `test_name_cut_and_hash`, `test_truncation_collision_resolves_by_hash`, `test_long_path_goes_to_long_paths`,
`test_header_has_full_source_name_when_cut`, `test_folder_rename_is_one_move`, `test_delete_and_prune_bottom_up_never_root`,
`test_stub_files_rendered_per_stub`, `test_source_rename_changes_rendered_hash`, `test_render_version_changes_hash`,
`test_diff_idempotent_property`.
**Expected-risk existing tests:** none.

## Task 20: Mirror repo and reconciliation service

**Files:**
- Repo: `{prefix}drive_transcript_mirror/{user_id}:{kind}:{source_item_id}` (`mirror_item_id`, `mirror_parent_id`, `mirror_name`,
  `mirror_ctag`, `rendered_hash`, `render_version`, `source_name`, `source_parent_id`, `state`), port + Firestore + in-memory,
  indexes, purge helper, smoke reads.
- `src/services/drive_transcript_mirror.py` (`DriveTranscriptMirror(repo, drive: UserDrivePort, localization, language)`;
  takes the port, never `UserDriveService`, REQ-ARCH-22): `reconcile(user_id, budget)` once per tick, last, under
  `MIRROR_OPS_PER_TICK` Graph writes and the deadline (Δ114); root creation: never adopt a folder — a taken localized name
  creates `<name> (Alek)` (Δ116); writes with `conflict="replace"`; every action recorded on its document only after Graph
  answered (idempotent); a 404 on any mirror write verifies the root with `get_change`; the record-delete path deletes the
  mirror file at once; the README file (OQ-T4) is part of the desired set.
- Tests: `tests/unit/services/test_drive_transcript_mirror.py` (scripted fake `UserDrivePort` recording every call).

**New tests:** `test_create_replace_move_rename_delete`, `test_one_folder_rename_one_move`,
`test_copy_second_mirror_file_no_new_transcription`, `test_crash_between_graph_answer_and_doc_write_reruns_safely`,
`test_users_same_named_folder_never_written_alek_suffix`, `test_ops_budget_bounds_a_tick`, `test_newest_first`,
`test_record_delete_deletes_mirror_file`, `test_readme_in_desired_set`, `test_writes_use_replace`.
**Expected-risk existing tests:** `tests/unit/services/test_user_drive_service*.py` must stay green (the service is untouched).

## Task 21: Audit, rotating scan, root recreate, tick integration, T2 docs

**Files:** the audit (every `MIRROR_AUDIT_DAYS`, `list_children_changes` one page per item call from the tick's item-call share:
missing file recreated, changed `cTag` rewritten from the store, moved/renamed moved back, a foreign item left alone, never
deleted or indexed, counted); the rotating drift scan (`MIRROR_SCAN_PAGE` documents per tick against their records); the
root's own deletion in `_apply` (`change.item_id == mirror_root_id`) → recreate, every document reset to `create`, newest
first; a crash between creating the root and saving its id leaves one orphan folder, found by the audit's name check and
reported (WARNING + counter), never adopted; tick wiring (mirror stage last, gated by both switches; the classification of
the root is **not** gated, it depends only on the stored id); purge interplay (G22); the skill's Transcripts-folder clause;
the sweep script gains the mirror collection; docs (README §4.23 mirror part, RFC status, CLAUDE.md, decision record).
**New tests:** `test_root_deleted_recreated`, `test_user_edit_repaired_at_audit`, `test_user_deletion_repaired_at_audit`,
`test_foreign_file_kept_and_counted`, `test_every_write_ends_under_root_invariant` (property over random scripted drives: every
Graph write's parent chain ends at `mirror_root_id`), `test_no_records_no_card_for_mirror_subtree` (end to end through delta
echoes), `test_orphan_root_after_crash_reported_not_adopted`, `test_scan_catches_unreported_drift`,
`test_mirror_not_run_when_switch_off_but_root_still_opaque`.
**Expected-risk existing tests:** `tests/unit/services/test_drive_indexing_*` that count Graph item calls per tick (the audit
uses the item-call share; it must not run when there is no mirror root).

**Size:** split for dispatch — **21A** audit + rotating scan + root recreate/orphan report (service + fakes); **21B** tick
wiring, purge interplay (G22 as resolved), skill clause, sweep script, docs. 21B depends on 21A.

## Task 22: T3 — summary agent, batched summary step, versions, header polish, docs

**Size:** split for dispatch — **22A** the agent (`drive_transcript_summary_agent.py`, `DriveTranscriptSummaryAgentConfig` +
`DRIVE_TRANSCRIPT_SUMMARY` in `agent_config.py`, the four upload JSONs `COGNITIVE_PROCESS_DRIVE_TRANSCRIPT_SUMMARY.json`,
`OUTPUT_FORMAT_DRIVE_TRANSCRIPT_SUMMARY.json`, `drive_transcript_summary.json`, `drive_transcript_summary_agent_v1.json`, parse
with `json.loads` and no regex fallback; the same "playbook steps that do not apply" note as Task 11); **22B** the stage's
summary step, store fields, `RENDER_VERSION` 2 header lines, docs. 22B depends on 22A.

**Files:** `src/agents/drive_transcript_summary_agent.py` (`drive_transcript_summary`, text only, its tokens and schema
`{summary: str}` ≤ `SUMMARY_MAX_CHARS`, the UI language, third person; input head/tail trimmed to `SUMMARY_INPUT_CHARS` with
the middle sampled by even stride and said so; no tools); prompt upload JSONs; `SUMMARY_VERSION = 1` stored on the transcript
store (`summary`, `summary_language`, `summary_version`, `summary_state`); the stage submits summaries through the same batch
port (batch document `kind = summary`), cost in the `transcribe` class, never indexed; the mirror re-renders once with the
"Summary" section labelled "automatic summary" by the renderer (D43); a failed/refused/held summary leaves the transcript final
and `summary_state = failed`, retried at the next `SUMMARY_VERSION`; OQ-T6: a UI-language change keeps the old summary until
`SUMMARY_VERSION` moves; header polish lines (`speaker labels may differ between parts of this recording` for any uncertain seam,
never for one window; the `~` stamps note); `RENDER_VERSION` 1 → 2 re-renders every mirror file from the store with no model
call. Docs: README, RFC status, CLAUDE.md, `src/agents/CLAUDE.md`.
**New tests:** `test_summary_failure_leaves_transcript_final`, `test_rerender_with_no_model_call`, `test_summary_language_is_ui_language`,
`test_summary_never_indexed`, `test_summary_input_trim_and_stride_note`, `test_labels_may_differ_line_only_multiwindow_uncertain`,
`test_tilde_note_only_when_interpolated`, `test_summary_cost_in_transcribe_class`, `test_language_change_keeps_old_summary`.
**Expected-risk existing tests:** the T2 render tests pinning header text (rendered hash moves with `RENDER_VERSION`; reviewer).

---

# Acceptance

**T1:** `make check` green; docker-gated tests (Task 9) run once and recorded; with the knob off a deployed tick changes media
records to `pending` and nothing else; **[OWNER]** eval gate run, bars recorded, Ukrainian reported separately, the T1 checks
of what T0 did not test answered (hours-long batch, 48 h file, expiry, unique audio > 22 min through the coverage check —
deciding 30 vs 45 min windows — and the short-clip early-timestamp effect on real recordings).
**T2:** the mirror tests green; on the owner's phone the first files appear under the localized folder with the right tree.
**T3:** summaries appear after transcripts; a forced summary failure leaves the transcript final.

# Owner steps (placeholders only; nothing here names a real resource)

1. Resolve Part C (or accept the proposals); the arbiter records the resolutions in the PR.
2. Review and merge the PR when the whole branch is green.
3. **Prompt layer** (paused window, back up first; `--apply` is human-only): upload `COGNITIVE_PROCESS_DRIVE_TRANSCRIBER`,
   `OUTPUT_FORMAT_DRIVE_TRANSCRIBER`, the `drive_transcriber` profile and blueprint (and, for T3, the
   `drive_transcript_summary` set); then pull the snapshot.
4. **Indexes:** `make deploy-indexes` (`config/firestore.indexes.json`); wait for `READY`; run
   `scripts/infrastructure/drive_index_smoke.py` (exercises the new reads). **Must precede step 7, and should precede step 5:**
   with the switch off no new composite query runs (Task 14's `test_no_new_composite_query_while_off`), but a query against a
   missing or building index is `FailedPrecondition`, systemic for the whole tick (`firestore_drive_index_repo.py:1269-1270`)
   — the moment the switch is on, every tick of the owner would fail until the index is `READY`.
   The prompt layer (step 3) likewise only has to exist before step 7: with the switch off nothing builds the prompt.
5. **Deploy OFF:** `.env` holds `DRIVE_TRANSCRIBE_ENABLED_DEV=off` (or unset). Pause the drive queue and the fan-out job;
   `make deploy` (main and indexer together; ffmpeg and `driveparser` are already in the image, `Dockerfile:11-13,34-37`);
   verify both services serve the new image; resume. The policy bump makes media records `pending` (metadata only).
6. **Backlog estimate** (read-only): `python scripts/infrastructure/drive_transcribe_backlog.py` → `scripts/memory/`.
7. **Morning enable for the owner only:** `DRIVE_TRANSCRIBE_ENABLED_DEV=on` in `.env`, `make deploy`; then
   `python scripts/infrastructure/drive_transcribe_flag.py --user <OWNER_USER_ID> --on --apply`. The son's flag stays False.
   **The overnight live test is bounded only if G34 is resolved first:** turning the flag on starts the whole backlog,
   newest first, under the $20 monthly cap — not one recording within $10. With G34's cap override, set
   `DRIVE_TRANSCRIBE_USD_CAP_DEV` to the night's budget (≤ 10) before this deploy; the hard cap with reservations then bounds
   the spend, and it is removed (unset → $20) only when the owner says so. Steps 3, 4 and this step are cloud writes the
   overnight authorisation must name explicitly (they are **[OWNER]** steps everywhere else in this plan).
8. Watch the tick line (`windows_*`, `transcripts`, `transcribe_usd`, `transcribe_reserved_usd`) and, after T2, the first mirror
   files on the phone; raise the cap only by a parameter change if the estimate says the backlog should finish sooner.
9. **Eval gate (T1, live, cents):** `python scripts/validation/drive_transcript_fixtures.py` then
   `python -m scripts.validation.drive_eval.transcript_eval --yes`; record the bars.
10. No rebuild, no `drive_initial_build_reset.py`.

# Rollback

1. `DRIVE_TRANSCRIBE_ENABLED_DEV=off` + `make deploy`: every transcript stage stops (no ffmpeg, upload, poll, mirror write); jobs,
   store and mirror stay; transcripts already indexed stay searchable and openable.
2. Per user: the flag script `--off --apply` (same effect for one user).
3. Previous image: every new field is plain and no enum gained a stored value, so old code parses every record; its upgrade pass
   re-classifies media to `kind_not_admitted` (policy 3 ≠ 2) and collects their chunks. Then run
   `scripts/infrastructure/drive_transcript_sweep.py --apply` to remove the job, batch, store and mirror documents old code does
   not know. Open Gemini batches finish unread and their files expire in 48 h; their reservations die with the sweep — but the
   `transcribe_reserved_usd` counter in the month's usage document does not (the sweep must zero it, or the month's
   transcribe headroom reads low after a roll-forward); their provider cost is incurred and never booked.
4. The mirror folder and its files are the user's after rollback; deleting them is the user's act (§27.13).

---

# Part C — Gate 2 gap list (open points; nothing here is silently decided)

Each item: what the RFC says or omits versus the code, the proposal, and whether it needs the owner.

- **G1. `download_url` belongs to T1, not T2.** §27.14 lists "port `conflict` mode and `download_url`" under T2, but T1's
  extraction reads the Graph URL (§27.4). *Proposed:* `download_url` in T1 (Task 8); `conflict` stays T2. Engineering.
- **G2. "Not read yet already counts pending media" is not true of the code.** §27.11 says the envelope already counts
  admitted media; `_unread_counters` passes `ADMITTED_KINDS`/`TRIAGED_KINDS` explicitly (`drive_indexing_service.py:640-645`)
  and `drive_search._content_kind` knows `CONTENT`/`TRIAGE` only (`drive_search.py:441-448`). *Proposed:* Task 5 adds
  `TRANSCRIBED_KINDS` to both. Engineering.
- **G3. A global policy with a per-user gate leaves the son's media `pending` forever.** Policy 3 admits media for every user
  (§27.3); the tick runs no stage for a user without the flag (Δ125). His recordings would read "not read yet" with no prospect.
  *Proposed:* keep the global policy (as the RFC); count media in `not_read_yet`/`unread` only for a user whose transcription is
  on (both gates). Alternative: make admission per user through `PlanContext` (then every flag flip needs a re-classification
  pass — rejected as heavier). Owner: confirm.
- **G4. The transcribe link would be extractor-stale on every upgrade pass (an endless loop).** `chain_for("audio")` returns
  `(PARSER,)` (`drive_extraction.py:32-33`) and `extractor_stale` marks any link not in the chain (`drive_index_planner.py:753-759`):
  every transcribed file would be re-planned, re-finalized and found stale again, so no pass ever applies. *Proposed:* the
  `transcribe-gemini` link on `TRANSCRIBED_KINDS` is never extractor-stale (Task 1); `TRANSCRIBE_VERSION` moves only through the
  requeue script (Δ122). Engineering, forced by the code.
- **G5. `CARD_VERSION` 4 → 5 re-embeds every card on the drive, not "only media cards".** `card_hash` hashes `CARD_VERSION` with
  the text (`drive_card.py:193-194`) and `_structure_stale` flags any card at another version (`drive_index_planner.py:748`).
  The new segment exists only on transcribed records, so no existing text changes. *Proposed:* no bump; precedent: §26.13's
  scene-labels segment shipped under 4 (the comment at `drive_index.py:17-20`). Cost of the RFC's way: a whole-drive card
  re-embed (embedding calls, minutes of ticks). Owner/arbiter: confirm.
- **G6. `render` and `summary` in `CURRENT_VERSIONS` would be decorative.** Δ124 adds them to the map, but no record-level
  criterion settles them; the mirror's `rendered_hash` and the store's `summary_version` already decide re-work per document.
  Adding them costs one no-op upgrade pass per bump and breaks the key-set pin (`test_drive_index_types.py:43-44`). *Proposed:*
  keep `RENDER_VERSION`/`SUMMARY_VERSION` as module constants checked per document; leave the map unchanged. Owner/arbiter.
- **G7. The price: the repo holds `gemini-3.8-flash` at its final $1.50/$7.50 on purpose.** Δ132 asks for $0.75/$3.75 until
  2026-12-31 as dated entries; the dated `PRICE_SCHEDULE` entry already exists (`price_consensus.py:101`) and the model is a
  `HOLD_FINAL_PRICE` member (`:115`) so cost is never under-reported. At the held price a batch hour books ≈ $0.16 now, so the
  $20 cap buys ≈ 125 h a month until 2027 instead of ≈ 250 h. *Proposed:* keep the hold (no price edit; Task 6 adds only
  `BATCH_DISCOUNT`); `TRANSCRIBE_HOUR_USD_ESTIMATE` $0.20 still fits. Owner: confirm or raise the cap.
- **G8. The open initial build would pay for transcription.** `available_usd` adds the initial balance to every class
  (`drive_enrich_plan.py:103-105`) and `_spend_write` spends the build first (`drive_indexing_service.py:2309-2312`); §27.10 says
  "no initial-build allowance". The owner's build is closed, but a future user's is not. *Proposed:* both exclude `transcribe`.
  Engineering.
- **G9. Reservations across a month turn.** A batch submitted on the 31st is collected on the 1st. *Proposed:* the reservation
  is recorded and released in the month it was made (`reserved_month` on job and batch docs); the real cost is booked in the
  month of collection ("recorded when collected", Δ105). The usage documents survive purges (§25.4.5), so a purge decrements the
  user's open reservations explicitly. Engineering.
  *Review correction:* booking the cost in the month of collection breaks the hard cap at a month turn — month M's
  reservation is released, and month M+1 receives dollars it never reserved, up to everything in flight on the 31st (Δ106's
  "batches in flight can never overshoot it"). *Revised proposal (adopted in Task 6):* the cost is recorded **when** collected
  (Δ105's timing) but **into the reservation's month** (`_spend_write(month=…)`). The Cabinet's "this month" line then shows a
  late-collected batch under the month it was bought in, which is the honest attribution.
- **G10. A batch document collection.** Δ97 names only the job document; "one poll per distinct batch" (Δ104) and "a batch that
  fails releases its reservations" need a per-batch record. *Proposed:* `{prefix}drive_transcript_batches`. Engineering.
- **G11. Window results off the job document.** Up to eight windows of segments would bloat a document every poll rewrites.
  *Proposed:* `windows/{index}` subcollection; the store gets the stitched result. Engineering.
- **G12. Copies in flight.** Δ112's cache covers a copy made after the first transcript exists; two copies first seen together
  would both be bought. *Proposed:* a second record with a shared key whose job is `working`/`waiting` waits for it (one
  composite index). Engineering.
- **G13. The kill switch must be a Cloud Build substitution.** §27.15 step 4 says it is "an `.env`-loaded optional knob, not a
  Cloud Build substitution", but the indexer's env is set with `--set-env-vars` (`cloudbuild-dev.yaml:141`), which replaces
  every variable on each deploy; a variable set by hand would vanish (the reason `DRIVE_UPGRADE_PASS` rides as a substitution,
  `:116-118`). *Proposed:* `_DRIVE_TRANSCRIBE_ENABLED` from `DRIVE_TRANSCRIBE_ENABLED_DEV`, default `off`, its own key
  (deploy-substitution trap). RFC wording to be corrected. Engineering.
- **G14. The demuxer allowlist as written is not valid ffmpeg syntax.** `-f mov,mp4,m4a,matroska,webm,…` (§27.4) is a list; `-f`
  takes one demuxer (whose own names contain commas: `mov,mp4,m4a,3gp,3g2,mj2`, `matroska,webm`). *Proposed:* `-format_whitelist`
  with the list on ffprobe and ffmpeg, plus `-f` pinned to the probed demuxer on the extraction run; a docker-gated test proves the
  image's ffmpeg honours both whitelists. Engineering. *Review note:* `format_whitelist` matches by `av_match_list`, so a
  short name (`mov`, `matroska`) admits the demuxer whose name lists it, and the whitelists are copied to nested inputs (a
  playlist's or `concat`'s children), which is what refuses them. The base image is `python:3.11-slim`, whose Debian release
  is not pinned; the docker test checks the shipped ffmpeg, not "bookworm". Two hardening additions in Task 9 of the same
  kind: `-tls_verify 1 -ca_file …` (ffmpeg does not verify certificates by default) and `-hide_banner -loglevel error -nostats`.
- **G15. The child launcher is duplicated, not shared.** REQ-ARCH-08 forbids the top-level `subprocess_content_extractor.py`
  from importing `drive_parser/`, and its tests patch module internals (`pwd`, `_effective_uid`, `asyncio.create_subprocess_exec`,
  `test_subprocess_extractor_sandbox.py:45-63`), so extracting a shared launcher would move code under existing tests.
  *Proposed:* the media extractor lives in `src/adapters/drive_parser/` (it may use `child.serve`), with its own launcher module;
  the two converge when sandbox S5 moves both parsers to the zero-credential service. Engineering; a known duplication of
  ~120 lines.
- **G16. The tick shares do not leave room for extraction.** Content classes use up to `1 − ENRICH_SHARE` (0.6) of the start budget
  (`:248-253`) and enrichment at least 0.4 (`:2385-2387`); a 25 % extraction window "after the enrichment phase" (§27.3) gets
  nothing on a busy tick. *Proposed:* only while the user has ready extraction work, content yields to `1 − ENRICH_SHARE −
  TRANSCRIBE_SHARE` and enrichment's stop leaves `TRANSCRIBE_SHARE`; otherwise both are unchanged (existing tests stay green).
  The deadline sum adds `TRANSCRIBE_UPLOAD_S` (a parameter the RFC implies but does not name). Engineering.
- **G17. The indexed body's language.** §27.8 a indexes "speaker labels and stamps included"; a localized label would make the
  index depend on the UI language. *Proposed:* the indexed and opened body use language-neutral `Speaker N` and stamps; the
  mirror file's header, stubs and summary are localized (T2/T3). Owner: confirm the label text.
- **G18. A body-layout change needs a re-index without re-buying audio.** `TRANSCRIBE_VERSION` never auto-upgrades (Δ122), but
  a change to how the body is rendered for the index changes the chunks. *Proposed:* `TRANSCRIPT_BODY_VERSION` on the record;
  a mismatch re-finalizes from the store automatically (embeddings only, no model). Engineering.
- **G19. Opening a four-hour transcript.** §27.11 returns "the transcript text"; 4 h is ≈ 300 000 characters. *Proposed:* at
  most `TRANSCRIPT_OPEN_MAX_CHARS` (60 000, the summary input bound) with a line naming the mirror file and suggesting a search
  for a passage. Owner: confirm the number.
- **G20. Silent recordings are not "unreadable".** A `no_speech`/`no_audio` stub is `metadata_only / no_text`, a reason in
  `UNREAD_REASONS` (`firestore_drive_index_repo.py:108`), so it would be counted "could not be read". *Proposed:* not unread
  (it was read); `failed` and `too_large` are. Engineering. *Review note:* the Firestore count (`unread_count_queries`,
  `:672-702`) filters by state, reason and kind and cannot see `transcript_stub`; the resolution is implementable without a
  new index only by giving the media kinds their own class list without `no_text` (every media `no_text` is a stub). Task 5
  now says so (port + both adapters + `access_counts`).
- **G21. The transcript state vocabulary.** §27.3 lists `none, ready, working, waiting, current, held, parked, failed` "as in
  §25.4.5", whose `held` means "a hold for this version" and which has `deferred` instead of `working/waiting`. *Proposed:* the
  table of Task 4 (`held` = a timed hold: cooldown, quiet period, with `transcript_hold` naming it; `parked` = the month's cap;
  `failed` = `MAX_ATTEMPTS`). Engineering. *Review correction:* the plan first listed "model backoff" as a `held` reason. §25.4.6
  as the code reads it says the opposite (`JobState`'s docstring, `drive_enrichment.py:36-38`: "an agent-type model backoff is
  NOT a job state … the backoff gates the queue read"); a record state would cost a write per ready file on every outage and
  a mass wake-up after it. Task 4 now keeps records `ready` and gates the stage.
- **G22. Opt-out and the mirror folder.** An opt-out purge deletes the sync state and with it `mirror_root_id`; the folder stays
  on the drive. On re-enable the bot creates `<name> (Alek)` and the old folder is then an ordinary user folder whose `.md` files
  are indexed as documents (duplicate hits of every transcript). *Proposed:* an opt-out purge with the drive still connected
  deletes the mirror root (bot-owned, recoverable from the provider's recycle bin); an access-loss purge cannot reach the drive
  and leaves it. Turning off `drive_transcribe_enabled` alone stops new work and keeps transcripts, store and mirror. Owner:
  decide both.
- **G23. The mirror root id must survive the tick's own state save.** `save_sync_state` merges every state field
  (`firestore_drive_index_repo.py:35-36,137`); a root created mid-tick would be overwritten by the end-of-tick save of the
  stale copy. *Proposed:* mirror fields excluded from `STATE_FIELDS`, written by a dedicated fenced method. Engineering.
  *Review correction of the reason:* the tick carries its own state object and the lease keeps any other tick out, so a
  root written into that object is saved, not clobbered; the clobber happens only if the root is written out of band. The
  real reasons for the dedicated write are (1) persisting the id the moment Graph answers, so a crash later in the tick does
  not leave the §27.9 orphan folder, and (2) once written out of band, no older snapshot (a lease-held script's save, the
  tick's own copy if it forgot to update it) may write it back. Only `mirror_root_id`/`mirror_root_name` need the exclusion;
  the audit/scan fields are tick-owned. Task 18 updated.
- **G24. Where the disclosure sentence goes.** §27.12 adds it to "§6's opt-in text" and also says the later Cabinet toggle carries
  it. Shown on the indexing opt-in, it would tell the son his recordings are transcribed. *Proposed:* the strings land now and
  are shown in the Drive section only while the user's flag is on; the toggle (later) reuses them. Owner: confirm.
- **G25. Gemini batch error classes.** §24.14 notes the Gemini adapter has no account-error recognition. *Proposed:* in the
  batch adapter, 429/5xx/network/auth/billing 4xx → uncharged outage (agent backoff); a per-response error or a refusal of one
  request's input → that window's charged attempt or gap marker. Engineering.
- **G26. The model id.** "Read from the adapter, never copied" (§27.5), but REQ-ARCH-23 forbids `gemini_batch_adapter.py` from
  importing `gemini_adapter.py`. *Proposed:* composition passes `GeminiAdapter.MODEL_TIERS[BALANCED]` into the batch adapter's
  constructor. Engineering.
- **G27. SDK version.** `requirements.txt:6` allows `google-genai>=2.0.0`; T0 measured the batch surface on 2.17.0. *Proposed:*
  raise the floor to 2.17.0 (no exact pin, so the main service is not moved further than needed); the wire tests pin the shapes.
  Engineering.
- **G28. Observability of batch calls.** Not going through `_call_llm` (Δ102) means no BigQuery `prompt_content` row and no Logfire
  span for a window. *Proposed:* accept for v1 (transcripts are other people's speech and should not be copied into BigQuery anyway,
  the §24.2 stance for scans); tick-line counters and per-batch INFO lines with counts only. Owner: confirm.
- **G29. How the owner's flag is set.** No Cabinet toggle in scope. *Proposed:* the dry-run-by-default flag script (Task 16).
- **G30. Gemini files on purge.** A purge cannot always reach the provider. *Proposed:* best-effort `delete_file`, logged; the
  Files API expiry (48 h) bounds the rest. Engineering.

**Added by the plan review (2026-10-20; undeclared deltas and gaps found against the current code):**

- **G31. An `EXTRACTION_VERSION` bump would strand every transcribed record (the G4 loop, second door).** `stale_versions`
  marks a record read under another `EXTRACTION_VERSION` (`drive_index_planner.py:719`); the upgrade re-plan makes it `pending`
  (`_outcome_stands` fails, `:258`), `content_due` is False for media (Task 1), so nothing ever re-reads it and the pass
  never applies. *Proposed:* the stage's store path re-finalizes a `current` record whose `extraction_version` is not the
  code's, from the store (embeddings only, no model), exactly as for G18's body version (Task 12 step 1). Engineering.
- **G32. Finalize is not a cheap step.** §27.3 puts finalize among the steps that "take no share", but a finalize embeds up to
  ≈ 125 chunks within `EMBED_FILE_BUDGET_S` (180 s). Ten files landing in one batch would take the tick. *Proposed:* each
  finalize starts only while `time_left()` and takes one `TICK_FILE_BUDGET` slot, like an index run; poll and collect stay
  share-free. Engineering.
- **G33. Results for records that are gone or re-versioned.** The plan never said what a collected window does when its record
  was deleted or edited while the batch ran; the reservation would be held forever and the job document orphaned. *Proposed:*
  settle (book the real cost, release the reservation, delete the Gemini file), drop the result, delete the job (Task 13).
  Engineering.
- **G34. The overnight live test is not bounded by anything in the plan.** The owner authorised one live test on his own
  recording within a $10 model budget. Turning the flag on starts the whole backlog newest first under the $20/month cap;
  the plan has no lever between "off" and "$20". *Options:* (a) an optional `DRIVE_TRANSCRIBE_USD_CAP` knob (`os.getenv`,
  deploy-only key + substitution) that can only **lower** `TRANSCRIBE_USD_PER_MONTH`; the hard cap with reservations then
  guarantees the bound, newest recordings first (Task 14 has the wiring "if accepted"); (b) an item allowlist on the flag
  script (`--only <item>`), which transcribes exactly the chosen recording but adds a per-user field and a planner branch.
  *Recommended:* (a), set to the night's budget; it reuses the cap logic the plan already tests. **Owner** (it changes what
  the authorisation buys: several recent recordings within the budget, not exactly one).
- **G35. A recording ffprobe cannot time.** Browser- and phone-recorded webm/mkv often carry no `Duration` and no `Cues`;
  ffprobe answers `N/A` and an input `-ss` must read from the start. The plan's step 3 ("probe, then windows") had no path for
  it. *Proposed:* such a file takes Δ134's one-pass `segment` path from the start, bounded by `-t TRANSCRIBE_MAX_SECONDS`,
  the duration read from the produced windows; reaching the bound is the `too_long` stub; the whole-file estimate (Δ106) uses
  the facet duration when present, else the `TRANSCRIBE_MAX_SECONDS` worst case, re-settled after the pass. Engineering.
- **G36. No (model, batch) `ModelUsage` leg.** Δ105 asks for "a `ModelUsage` leg keyed by (model, batch)" through "a
  runner-scoped ledger". The plan prices each result with `BatchUsage.cost()` and bills it directly via
  `QuotaService.record_usage(account_id, model, tokens, cost)` (`ports/quota_service.py:10`), which already takes a cost; the
  modality split lives in `BatchUsage`. Same money, one type fewer. Engineering (declared, not silent).
- **G37. Retry rules per check, as the RFC words them.** The plan first gave every collect-time failure `MAX_ATTEMPTS`
  resubmits then a gap marker. §27.6 says the time validator gets **one** resubmit then the gap marker, and an interpolation
  share above 0.3 is resubmitted **once** (then accepted with `~` stamps); §27.5 gives coverage and invalid output
  `MAX_ATTEMPTS`. After `MAX_ATTEMPTS` the RFC does not say "gap marker" for those two; the plan's gap marker + `truncated`
  (instead of failing a long file for one bad window) is the declared choice. Engineering.
- **G38. A date said in a recording would re-date it.** §27.8 a: the record's `effective_date` "stays §3.1". The code moves it
  to a profile's verified `issued` date when the profile lands (`drive_enrich_plan.py:203,259-286` → `effective_from`, `:125`)
  and at `indexed()` (`drive_index_planner.py:927`); a transcript's profile reads dates out of speech. *Proposed:* media kinds
  are exempt in both (Task 5). Engineering.
- **G39. Reserve when the file fits, not when it is submitted.** §27.5 reserves "when the batch is submitted", after a fit check
  made before extraction. A long file is extracted over several ticks; a second file checked meanwhile sees no reservation
  for the first and passes too, and the two submissions together exceed the cap — the soft cap Δ106 exists to prevent.
  *Proposed:* the fit check and the reservation are one step, before the first window (Task 4, Task 12 step 4); park,
  failure and abandon release it. Engineering; it serves Δ106's purpose more strictly than its wording.


## §27.14 items mapped

| §27.14 item | Task |
|---|---|
| T1 build: `Admission.TRANSCRIBE`, policy 3 | 1 |
| T1 build: job document and fields | 1, 7 |
| T1 build: `MediaAudioPort` + child ops + hardening | 9 |
| T1 build: `BatchTranscriptionPort` + Gemini adapter | 10 |
| T1 build: the agent | 11 |
| T1 build: windows, stitch and speaker alignment (T4 folded in) | 2, 3, 13 |
| T1 build: the store and cache | 7, 12, 13 |
| T1 build: cost + `UsageClass.TRANSCRIBE` + cap | 4, 6, 12, 13 |
| T1 build: card | 5 |
| T1 build: `open_file` | 15 |
| T1 build: skill and capabilities lines | 15 (folder clause 21) |
| T1 build: kill switch | 14 |
| T1 build: disclosure text | 15 (G24) |
| domain: window plan (boundaries, overlap, last short window) | 2 |
| domain: seam cut and de-duplication; time rebase | 3 |
| domain: speaker alignment (matched, unmatched, silent-through-overlap, uncertain seam) | 3 |
| domain: interpolation (tilde, share bound) | 3 |
| domain: validator (non-monotonic times) | 2 |
| domain: content key (quickXor, none; no sha1) | 2 |
| domain: coverage check (short window = failed attempt) | 2 (pure), 13 (resubmit) |
| domain: `MM:SS`/`H:MM:SS` parse | 2 |
| domain: state machine (transitions, hold/park/fail, month boundary, whole-file cap hold, reset on content version) | 4 |
| domain: render of every stub | 3 (structural), 19 (localized mirror file) |
| services: submit/poll/collect | 12, 13 |
| services: resume after a killed tick | 12 |
| services: expiry resubmit | 13 |
| services: cache hit with no model call | 12 |
| services: outage backoff | 12, 14 |
| services: copy shares one transcription | 12 |
| services: edit cooldown | 4, 12 |
| services: deadline-sum assertion | 14 |
| adapters: wire tests upload/submit/poll/collect/delete, usage by modality | 10 |
| child: argv hardening pinned; hostile-input set under the rlimits | 9 |
| billing: audio rate, batch multiplier, ledger leg, hash pin of price rows | 6 |
| T1 check: batch waiting for hours | 13 (fake clock), owner eval (live) |
| T1 check: window file older than 48 h (re-upload) | 13 |
| T1 check: job expiry and resubmit | 13 |
| T1 check: unique audio > 22 min through the coverage check | 16 (fixture), owner eval |
| T1 check: short-clip early timestamps on real recordings | 16 (fixture), owner eval |
| Expected-to-break list (media as `kind_not_admitted`, version pins, `CURRENT_VERSIONS`) | 1, 5, 6 risk lists |
| T2 build: mirror documents and service | 19, 20 |
| T2 build: port `conflict` mode | 18 |
| T2 build: `download_url` | 8 (moved to T1, G1) |
| T2 build: `inside_opaque` for the root | 18 |
| T2 build: reconciliation; audit and scan | 20; 21 |
| T2 build: localization wiring on the indexer; the README | 18; 19, 20 |
| T2 tests: create/replace/move/rename/delete; one folder rename = one move | 20 |
| T2 tests: copy = second file, no new transcription | 20 (with 12) |
| T2 tests: crash between Graph answer and document write | 20 |
| T2 tests: root deleted → recreated; user edit and deletion repaired at the audit; foreign file kept and counted | 21 |
| T2 tests: collision and long-name rules | 19 |
| T2 tests: every write ends under the root | 21 |
| T2 tests: no records and no card for the mirror subtree | 18, 21 |
| T2 tests: a same-named user folder is never written into (`(Alek)`) | 20 |
| T2 tests: crash between creating the root and saving its id → one orphan, reported, never adopted | 21 |
| T3 build: summary agent and batch step, `SUMMARY_VERSION`, `RENDER_VERSION` re-render, header lines | 22 |
| T3 tests: summary failure leaves transcript final; re-render with no model call; summary language | 22 |
| Eval: fixture script, metrics, Ukrainian separate, bars from the first live run | 16 (built), owner (run) |

**Not mapped as written:** the RFC's `CARD_VERSION` 4 → 5 and the `render`/`summary` map keys (G5, G6: proposed not to do);
"`-f` with a demuxer list" (G14: replaced by `-format_whitelist`); "bars recorded from the first live run" and the two live T1
checks are owner acts, not agent tasks; the Cabinet transcription toggle is explicitly later (§27.12). Also not as worded:
"reserved when the batch is submitted" (G39: reserved when the file fits), the cost month at a month turn (G9 revised), the
(model, batch) ledger leg (G36), and the per-check retry counts after `MAX_ATTEMPTS` (G37).

## Task order and dependencies

T1: 1 → 2 → 3; 2 → 4 (Task 4's `ctx` carries the month's dollars as plain floats, so it needs no `UsageClass.TRANSCRIBE`);
1 + 3 → 5; 4 → 6; 5 → 6 (both edit `drive_enrich_plan.py`: G38 and G8); 1 + 5 + 6 → 7A → 7B (5 changes the port's
`count_unread`); 8 and 9 and 10 are independent of each other (9 needs 2's types; 10 needs 6's `BatchUsage`); 10 → 11;
4 + 7B + 8 + 9 + 11 → 12 → 13 → 14A → 14B; 3 + 7B + 14B → 15; 7B + 14B → 16; everything → 17.
T2 (after 17): 18 → 19 → 20 → 21A → 21B. T3: 21B → 22A → 22B.
Parallel-safe group for dispatch: {8, 9, 10} — each in its own worktree only if the arbiter integrates and runs the full
`make check` on the assembled branch (memory `feedback_plan_authoring_via_subagents`). {5, 6} is no longer parallel-safe
(shared file).
