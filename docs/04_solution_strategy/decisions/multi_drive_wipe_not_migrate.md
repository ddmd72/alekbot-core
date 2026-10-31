# Multi-drive slice 1b ships by wipe and rebuild, not by migration

**Date:** 2026-10-30
**Status:** Decided (owner); built as `scripts/infrastructure/drive_wipe.py`
**Scope:** the slice 1b deploy of `docs/10_rfcs/MULTI_DRIVE_RFC.md` (§12, D20)

## Decision

At the slice 1b deploy all of a user's drive data is deleted, the drive is reconnected in the Cabinet and the index is
rebuilt from zero. There is no migration, no cut-over script and no rollback. Only the facts database and the Gmail index
are precious. The wipe removes index records, chunks, hints, transcript jobs, batches and the store, sync states, mirror
roots and ledger, owner drive notes, initial-build records, connections and settings; it keeps the monthly model usage,
the credentials and the note erase requests.

## Rejected

- **A cut-over script with copy-back and purge-new** (RFC rev 1 to 1.2): copy transcripts, ledger, roots, notes and the
  initial-build record to re-keyed documents, purge the old index with keep, and a `--purge-new` / `--copy-back` pair to
  return to the old revision. Rejected: it is the largest piece of the slice (a preflight, key-equality rules I6/I7, a
  re-attachment stamp, a rollback matrix) for data that is cheap to rebuild.
- **Teaching the new code the old key shapes:** a legacy branch in `src/` that the bar of the programme forbids (D12).

## Why

One user holds drive data. What is lost is re-derivable: the index is a rebuildable mirror; a recording is transcribed
again, at a cost the owner accepts; a drive note is written again. The migration machinery would have carried more risk
than the loss it prevents.

## Consequences

Search answers from a growing index with its coverage line; every `drive:` ref written before the deploy answers "search
again" (D13); transcripts and drive notes start empty. The previous revision cannot read the new documents.

## Revisit when

A second user has drive data worth keeping (paid transcripts or notes in volume), or a later slice changes a key of a
store that cannot be rebuilt. Until then a re-key of a drive store is a wipe.
