# Decision: Audio and video are transcribed by a stage with its own job model (Gemini Batch), behind two switches and a hard cap

**Date:** 2026-10-21
**Status:** Accepted; T1 built on `feat/drive-transcripts`, not deployed. T2 (mirror folder) and T3 (summary) not built.
**Supersedes:** [drive_audio_video_deferred.md](drive_audio_video_deferred.md) for audio and video (that record said transcripts would
arrive "through the extractor seam"; they did not, see below). It stays correct for everything else it names.
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §27 (owner decisions D41–D47 in §21; the 38 engineering
deltas of the build are Δ136–Δ173 in §27.16 and the as-built summary is §27.19); owner's map
[user_drive/README.md](../../05_building_blocks/user_drive/README.md) §4.23; rollout `docs/07_deployment/README.md` → "Transcripts of recordings (§27)".

## Decision

1. **Audio and video are transcribed.** The policy admits them for a new mode, `Admission.TRANSCRIBE` (`POLICY_VERSION` 3); the transcript is
   indexed through the ordinary text path, so a recording is found by what was said and Smart opens its transcript.
2. **A job with states on the record, advanced a step at a time across ticks, not an extractor chain link.** A batch answers hours later;
   one `extract()` run cannot wait (lease, file budget, the §8.2 crash rule). The job document, batch documents, the content-keyed store and the
   window results are separate collections; the record keeps only plain fields (no enum value added, so the previous image still parses it).
3. **Gemini Flash on the Batch API**, half price on input and output, one request per ≈ 30-minute window with a 60 s overlap, windows
   independent (stitched and speaker-aligned by code). Never flash-lite.
4. **Two switches, both off by default:** the indexer kill switch `DRIVE_TRANSCRIBE_ENABLED` (a Cloud Build substitution, from the deploy-only
   `DRIVE_TRANSCRIBE_ENABLED_DEV`) and the user's own flag `drive_transcribe_enabled` (set by the owner's script; no Cabinet toggle yet). Consent is
   per user and explicit; indexing alone sends no recording anywhere.
5. **A hard monthly cap with reservations:** `TRANSCRIBE_USD_PER_MONTH` $20; a file's estimate is reserved before its first window, settled at
   collection in the reservation's month; a file that does not fit is parked whole until next month. An optional `DRIVE_TRANSCRIBE_USD_CAP`
   can only lower the cap.
6. **The held price stays in billing** (`gemini-3.8-flash` at its final price), so a batch hour books ≈ $0.16, and $20 buys ≈ 125 hours until 2027.
7. **No `CARD_VERSION` bump, no `render`/`summary` map keys, `STRUCTURE_POLICY_SINCE` = 2.** A media-only change must not redo work for
   documents or folders.

## Alternatives rejected

- **The extractor chain (`audio → [transcriber]`).** The synchronous `extract()` contract cannot wait for a batch; holding a run open for a day
  holds the file budget and the lease and looks like a crash. This is the part of the earlier deferral record that did not survive contact with the batch API.
- **Synchronous (interactive) transcription.** Twice the price and a long call per window inside a tick. The owner asked for half price (D42/D44).
- **Sequential windows with the previous window as context** (the brief). Window *k+1* would wait for *k*: up to 24 h per window for a long file, and
  the provider's 48 h file retention would run out. Independent windows plus code-side alignment instead.
- **ffmpeg `segment` muxer in one pass** for a file with no duration. It cannot produce the 60 s overlap stitching needs. Replaced by a
  local-intermediate pass (remote → one local Opus file → stream-copy cuts) of at most three windows.
- **A global switch only.** The decoder surface of ffmpeg on other people's files and the consent both argue for a per-user flag too; a second
  user with drive indexing exists and stays off.
- **Bumping `CARD_VERSION`.** It hashes into every card: a whole-drive re-embed for a segment only transcribed records have.
- **Pricing at the promotional rate.** It would under-report cost for the sake of 2× the hours. Never under-report; the owner can raise the cap.
- **Reserve at submission.** A long file is extracted over several ticks; two files could pass the cap together. Reserve when the file fits.
- **A per-file allowlist for the first live run** (instead of the lowering cap knob). Needs a per-user field and a planner branch; the knob reuses the cap logic.

## Consequences and costs

- **Cost.** About $0.16 per audio hour at the held price; $20/month ≈ 125 hours; the first run is bounded by the cap knob. A window's reservation is
  $0.30/h plus a prompt allowance and its output is capped, so spend never exceeds what was reserved; over-reserving only limits what is in flight.
- **Not observable in BigQuery or Logfire.** A batch is not a call: no `prompt_content` row and no span per window; the tick line and batch log lines
  carry counts. Transcripts are other people's speech and are never logged.
- **Privacy.** Audio goes to Google only and is deleted from the Files API after collection; an opt-out purge removes the index side; a rebuild keeps the
  stored transcripts so audio is not bought twice. The purge books (not releases) a reservation that backs audio still running at the provider.
- **Residuals.** A drop ffmpeg's reconnect cannot recover, in the re-run without `-xerror`, reads as the end of the file; a crash inside a single write can
  still double-book a batch; speaker alignment on real multi-window audio is unmeasured until the live eval.
- **Open for T2.** Rulings recorded but not built: an opt-out purge with the drive connected deletes the bot's own mirror folder (recoverable from the recycle bin);
  turning transcription off keeps transcripts and the mirror.

## Rulings that shaped the build (full list: RFC §27.16, Δ136–Δ173)

Held price (T-R1); mirror-folder opt-out (T-R2); lowering cap knob (T-R3); structure decisions decoupled from the policy version (T-R5); the stored
type is `StoredTranscript` (T-R6); interpolation spans to the next stamp of any speaker (T-R7); resubmits must fit and reserve (T-R8); cooldown anchored on
purchase (T-R9); per-submission reservations on the batch document (T-R11); env-prefixed subcollections (T-R13); orphan sweep as its own port method
(T-R14); local-intermediate pass (T-R15) and the ffmpeg exit-code rule (T-R16); per-response errors are charged attempts (T-R18); per-window output
cap and the $0.30/h estimate (T-R19); a job with a submitted window is left to collection (T-R20); retry backoff for charged attempts (T-R21); batch booking
idempotency (T-R22); all-gap files are not finalized (T-R23); card-capped records are not candidates (T-R24); the purge books in-flight reservations (T-R25).

**Trigger to revisit:** the zero-credential parser service exists (turn the flag on for a second user), the first live eval records its bars (revisit
the window length and the cap), or the promotional price window ends and the held price stops being conservative.
