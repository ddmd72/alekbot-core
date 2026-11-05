"""
The "same read" check of a parser upgrade's re-read (docs/10_rfcs/EXTERNAL_FILES_RFC.md §7 U5, A-U5-1: Layer 2). Pure.

A record whose `extracted_by` names an older parser version is read again (`drive_index_planner.extractor_stale`).
This module never decides WHETHER a record is read again, only how a finished re-read may commit: when the new read,
chunked with today's chunker, gives exactly the stored chunk sequence of the readable generation and every record
field the text decides, the run commits `drive_index_planner.reread_unchanged` (a stamp: no new generation, no chunk
write, no embedding). Any inequality takes the full index path, where the chunk-hash vector reuse (Layer 1) still
embeds only changed chunk texts.

Public interface:
    same_read(rec, stored, chunked, outcome, *, space) -> bool
    changed_chunks(stored, chunked) -> Tuple[int, ...]
"""
from __future__ import annotations

from typing import Sequence, Tuple

from .document_extraction import ExtractionStatus
from .drive_chunker import ChunkedText
from .drive_extraction import ExtractionOutcome
from .drive_index import DriveChunk, DriveIndexRecord


def same_read(rec: DriveIndexRecord, stored: Sequence[DriveChunk], chunked: ChunkedText, outcome: ExtractionOutcome,
              *, space: str) -> bool:
    """True only when committing `outcome` as a stamp loses nothing: `rec` has a readable generation in `space` (the
    tick's embedding space) and was indexed at its current content version (an upgrade re-read, not an edit);
    `outcome` is text; `stored` is that generation's chunks, `chunked` the new text's chunks, and they are the same
    sequence (count, index 0..n-1, text and offsets of each); and the record fields the text decides are equal:
    `text_chars`, `pages_total`, `truncated` (the chunker's cut included), `script_rendered` and the settled
    `capture` / `capture_source` (the caller passes the outcome after `file_capture` settled it)."""
    if rec.indexed_generation is None or not rec.chunk_space or rec.chunk_space != space:
        return False
    if not rec.content_version or rec.indexed_version != rec.content_version:
        return False
    if outcome.status is not ExtractionStatus.TEXT:
        return False
    if not (len(stored) == len(chunked.chunks) == rec.chunk_count) or rec.chunk_count == 0:
        return False
    ordered = sorted(stored, key=lambda c: c.index)
    for position, (old, new) in enumerate(zip(ordered, chunked.chunks)):
        if (old.index != position or old.generation != rec.indexed_generation or old.text != new.text
                or old.char_start != new.char_start or old.char_end != new.char_end):
            return False
    return (rec.text_chars == len(outcome.text) and rec.pages_total == outcome.pages_total
            and rec.truncated == (outcome.truncated or chunked.truncated)
            and rec.script_rendered == outcome.script_rendered
            and rec.capture == outcome.capture and rec.capture_source == outcome.capture_source)


def changed_chunks(stored: Sequence[DriveChunk], chunked: ChunkedText) -> Tuple[int, ...]:
    """The indexes of the new chunks whose text no stored chunk holds: what Layer 1 would embed (before it merges equal
    new texts into one call). For the read-only upgrade probe; empty when every new text is already stored."""
    known = {c.text for c in stored}
    return tuple(i for i, chunk in enumerate(chunked.chunks) if chunk.text not in known)
