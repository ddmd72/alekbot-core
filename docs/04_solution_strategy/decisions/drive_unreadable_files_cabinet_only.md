# Files the index could not read are shown in the Cabinet, not told to the bot

**Date:** 2026-11-02
**Status:** Built; pending deploy
**Scope:** drive search envelope and coverage line; the Cabinet's drive card (`USER_DRIVE_INDEXING_RFC` §6, §10.2).

## Decision

The count of documents that "could not be read" (`failed`, or `metadata_only` for `too_large`, `no_text`, `machine_text`,
`user_chunk_cap`) is removed from everything the bot sees: `DriveSearchEnvelope.unread` and `CoverageFacts.unread` are
deleted, with their lines. The bot is told only about the transient "not read yet" (`not_read_yet`). The owner sees the
files instead: the drive card lists up to 5 by name, with the reason and a link, and "+ N more". The indexer's `unread`
counter and `count_unread` stay (the Cabinet total is `unread` minus `not_read_yet`).

## Why

The number is permanent and drive-wide, so it sat on every answer and made each one hedge. A scan with no text layer will
never be read; telling the bot again and again changes no answer. It is the same reason files without passage keywords by
policy were already left out of the coverage line (UAT 2026-10-14). The owner can act on it (a better scan, a smaller file),
and the Cabinet is where the owner looks.

## Rejected

- **Zeroing the field:** a field that always reads 0 is a lie in the type and keeps the dead line and its tests.
- **Showing the files only on an empty result:** the bot would still carry the count on every other answer path.
- **Keeping the count for the bot:** the problem above.

## Consequence

A question about one specific unreadable file still sees what its card says (its name and index state); the bot just
does not volunteer the drive-wide number. The list uses the filters of the counts, so no composite index is added, and the kind sets of the indexer's counter:
recordings join only when the transcription kill switch (`transcribe_switch_on` of `DriveSearchService`) and the
connection's own flag are both on.
