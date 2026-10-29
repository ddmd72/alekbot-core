# Decision: Transcripts are mirrored into a bot-owned `_Transcripts` folder; a deleted recording's transcript is archived, never deleted

**Date:** 2026-10-21
**Status:** Live since 2026-10-21, owner only (owner decisions D48, D49, OQ-T10). Deploy gate: the Δ217 composite indexes `READY` (Δ230).
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §27.9 (design), §27.16 (rows Δ183–Δ244), §27.19 ("T2 as built");
owner's map [user_drive/README.md](../../05_building_blocks/user_drive/README.md) §4.23; rollout notes (ops); T1 record [drive_transcripts_t1.md](drive_transcripts_t1.md).

**Since DRIVE_SPECIALIST_RFC:** Smart no longer holds the drive intents. `search_files_in_drive` is gone; the drive specialist (`work_with_drive_files`, [drive_specialist.md](drive_specialist.md)) searches with `search_drive`, and `drive_deleted` became its tool `search_deleted_recordings_and_notes`. The text below is kept as decided.

## Decision

1. **A mirror folder (D42, revised).** For a user with both transcription switches on, the indexer writes one `<source name>.md` per transcript into a
   bot-owned folder at the App Folder root, following the source tree, so the transcripts are readable on the owner's phone without the bot.
2. **D49 — the folder says what it is.** The root is `_Transcripts` in every UI language (the underscore marks a system folder the bot may rewrite),
   the archive is `Archive`, the long-path folder `Long paths`, and a short localized `_README.md` says: managed by the bot, files may be rewritten, do
   not edit, transcripts of deleted recordings are in `Archive/`. Fixed names remove every language-dependent rename and collision (Δ215).
3. **D48 — a deleted recording's transcript is archived, never deleted.** After a complete round and `MIRROR_ARCHIVE_GRACE` (1 h) a deleted source's file
   moves to `_Transcripts/Archive/<YYYY-MM-DD>/<path>.md` (the day in the user's zone), keeps its store entry pinned, and is reached only by an explicit
   "search in deleted files" (`drive_deleted` on `search_files_in_drive`, ledger-backed, works with drive search off). A recording moved out of the
   index's scope is **detached** (in place, pinned), not archived. A restore moves the file back. The bot never deletes an archived file; the owner does.
4. **OQ-T10 — an opt-out deletes nothing on the drive.** The index side is purged; the root document, the ledger and the pinned store entries stay
   (no purge deletes them), and a re-enable re-identifies the root and re-buys only the active transcripts.
5. **The root's identity lives in a document no purge deletes** (`drive_mirror_roots`), and the ledger (`drive_transcript_mirror`) is the bot's record of
   what it wrote. Without them a purge would make the bot create a second root and index the first as ordinary documents.
6. **Writes are idempotent and never destroy the owner's files** (Δ191, Δ210, Δ216): `conflictBehavior=fail`, an intent recorded before a create and read
   back by name, adoption only of an item created by the app at or after the intent with the rendered size, a numbered suffix for a foreign name.
   Every write is checked to end under the current root (Δ225). The chat agent cannot write into the folder (Δ232).
7. **A mirror error never stalls the index.** Errors are charged per file and park it after `MIRROR_MAX_ATTEMPTS` (Δ204, Δ220); only the systemic
   classes end the tick. The step runs last, with its own budgets and a reserved tail so a T1 backlog cannot starve it (Δ209, Δ226, Δ227).

## Owner rulings of 2026-10-21 (after the final reviews)

1. **D48, as amended: the files belong to the bot.** The user does not edit transcript files on the drive. Every `.md` in `_Transcripts`, active **and archived**, is
   overwritten by the bot (at the weekly audit at the latest); a rename or a move inside the folder is moved back. To keep a custom version, copy the file to another folder.
   Deleting an archived file makes the bot forget it (the document is deleted, the pin released); moving a file out of `_Transcripts` makes it the owner's copy. This
   **reverses** the earlier "an archived file is only followed, never rewritten" (§27.9.9) and the read-me's "do not edit" wording is now backed by behaviour (Δ241, built).
   The bot still never deletes or re-creates an archived file. The read-me and the Cabinet `drive.mirror_clause` say it in the four languages. Detached files (a recording that
   left the index's scope; the file stays in place) are no exception: they keep their stored copy under a pin, so they are overwritten and moved back the same way (Δ243).
2. **No mirror-only off switch.** The mirror is part of transcription. The stop lever is the user's transcription flag or the kill switch, and both also stop paid
   transcription, batches in flight included. The runbook states this side effect.

## Alternatives rejected

- **Delete the mirror file with its source (D42's first rule).** Loses the owner's transcripts of recordings he deleted on purpose or by accident; the
  text holds no voiceprint and the use is personal (D48).
- **Delete the mirror folder on an opt-out (T-R2, Δ153).** Destroys visible files the owner may rely on; a re-enable would also need to re-create them
  (OQ-T10, Δ185).
- **A UI-language folder name (D42, Δ120).** A language change would rename the root and could collide with the owner's own folders (D49, Δ215).
- **The root id on the sync state.** All four purges delete the sync state; the next tick would create a second root and index the first (Δ184).
- **Comparing every record with every mirror document each tick.** About 1.4 million reads a day for 5 000 transcripts; record flags, a seed pass and
  durable deletion marks replace it (Δ187, Δ188).
- **Adopting a folder or file by name.** An owner's `_Transcripts` or `_README.md` is never taken for the bot's (Δ116, Δ223).
- **A confirming read per archived file.** A deleted folder of 1 000 recordings would cost 100 ticks; the archive is a reversible move (Δ211).
- **Leaving an edited archived file alone (the first D48 audit rule).** The stored copy is what open and the deleted-files search serve, so the file on the drive and the bot's
  answer would disagree; the owner ruled that the bot's copy wins (ruling 1).
- **A mirror-only `os.getenv` switch.** Declined by the owner (ruling 2): the mirror is not a separate product.
- **Re-creating an archived file the owner deleted.** The record is gone and the header cannot be rendered; the document and its pin are released (Δ224).
- **Charging nothing for an unexpected error.** One bug in one file would hold the head of the queue for ever; it is charged and parks (Δ220).
- **A stored back-off for root setup.** No root document exists to hold it and a sync-state field would be a schema change; it lives in process memory
  (Δ227).

## Consequences and costs

- **Pace.** At most 40 Graph writes, 10 reads and 200 store stamps a tick; 5 000 first-time files take about 22 hours. No continuation is requested.
- **Index dependency.** The indexer wires the ledger always (one `roots` query per tick); the Δ217 composite indexes (10 on the ledger) must be `READY`
  before the revision serves (Δ230), or the owner's mirror does nothing (the tick goes on, Δ236). The purge's pin reads need no composite (Δ237).
- **Re-enable cost.** About $0.16 per audio hour at the held price for every active transcript, newest first, under the monthly cap (live $10, §27.9.12).
- **Rollback.** Pause the queue and the fan-out, move `_Transcripts` out of the App Folder, deploy the old image, resume; reverse after the roll-forward. A T1
  image loses the pins within 30 days (named residual). `drive_transcript_sweep.py` keeps the mirror's data unless `--mirror`; `--mirror --apply` also needs `--after-rollback` and the owner's prior removal of `_Transcripts` from the App Folder (Δ229).
- **Residuals.** A restart forgets the root-setup hold; a folder moved out and not yet observed gets one write; a later orphan root folder is not reported;
  a persistent root-loss reset bug stalls that user's mirror; the audit writes one document per file per week (Δ222–Δ225).
- **Owner steps** S3–S6 (indexes, deploy, live check, Cabinet texts). Tasks 21C-2/21C-3 (Δ228) are built: `MirrorHost.stop_exceptions`, the opt-out raised as itself, `seed_version()` in the domain, no op for an empty move-back PATCH, and a blocked move-back of a document with a pending gone mark is charged with back-off (`charged_doc`, then `failed_at` + `MIRROR_FAILED_RETRY`), so a backed-off or parked mark takes no reserve.

## Rulings that shaped the build

Δ218 (ledger key has no drive id), Δ219 (`long_path_source`, `pending_size`, clock skew, host zone and language), Δ220 (unexpected errors are charged),
Δ221 (root names read first), Δ222 (audit stamp and cursor sentinels), Δ223 (read-me intent, blocked cadence, all-or-retry reset, `REDO_DIGEST`),
Δ224 (detached documents frozen, confirmed listing 404), Δ225 (strict inside-root check), Δ226–Δ227 (reserve window, root-setup hold), Δ229 (scripts),
Δ230 (index gate), Δ231 (root-mark callback), Δ232 (deleted-files search and chat guard), Δ233 (no names in logs), Δ234 (tests).

**Trigger to revisit:** the first live run (S5) shows the pace, the number of audit writes or the Graph read cost is wrong; a second user gets
transcription (consent and retention rule, OQ-T3); the owner wants the summary (T3) in the mirror file's header.
