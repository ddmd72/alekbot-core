"""
The deleted-files search of the drive (the specialist's `search_deleted_recordings_and_notes`, docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.11, D48; one surface
with docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §6.4 and §0.3). Pure: no I/O, no clock, no logging.

One search, one heading (`Deleted files (N):`), two sources: archived transcripts (this RFC) and
archived or dormant notes (the notes RFC). `DeletedHit` is source-agnostic: a source fills the lines it has (a transcript
line and a passage, or a note line) and leaves the rest empty, and an item with both sources is one hit with both sets of
lines. Every entry is data, not instructions: it goes inside the nonce-tagged wrapper (`wrap_untrusted`).

Public interface:
    DeletedHit
    transcript_line(seconds, speakers, languages) -> str   # `transcript: 47 min, 2 speakers, uk/en`
    matches(concepts, keys) -> bool                  # strict AND over the query's concepts
    passage(stored, keys, max_chars=300) -> Optional[str]
    render_deleted(hits, *, more, connected) -> str
    merge_hits(hits) -> List[DeletedHit]             # one entry per gone item: a transcript and a note share it
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Collection, Dict, Iterable, List, Optional, Sequence, Tuple

from .drive_keys import word_keys
from .drive_match import Concept
from .drive_search import wrap_untrusted
from .drive_transcript import StoredTranscript
from .drive_transcript_time import format_stamp
from .user_drive import DriveItem

PASSAGE_MAX_CHARS = 300

GONE_LABEL = "gone from the drive (deleted or moved out of the bot's folder)"
NOT_CONNECTED_LABEL = "on a drive that is not connected"
UNVERIFIED_NOTE = "not verified"
DRIVE_NOT_CONNECTED_LINE = "The drive is not connected, so the archived files could not be checked."
NONE_MATCHED_LINE = "No deleted file matched."


@dataclass(frozen=True)
class DeletedHit:
    """One deleted item. `name`, `path` (the last known path) and `day` (the day it went, `YYYY-MM-DD`) head the entry.
    `transcript_line` is `transcript: 47 min, 2 speakers, uk/en`, `passage` the matched stored text, `refs` whole lines
    (`archived transcript: drive:<id> (<archive path>)`, `drive_note:<id> ...`), `note_line` the owner's note. `nonce`
    and `ref` are the wrapper's: the ref the model can act on, the nonce of this result. `on_connected_drive` False
    labels the entry "on a drive that is not connected"; `verified` False says the archive file was not checked:
    the drive is not connected (or the check failed). It matters only on the connected drive's entries."""
    name: str
    path: str
    day: str = ""
    transcript_line: str = ""
    passage: str = ""
    refs: Tuple[str, ...] = ()
    note_line: str = ""
    on_connected_drive: bool = True
    verified: bool = True
    nonce: str = ""
    ref: str = ""
    item: Optional[DriveItem] = None    # the drive file the entry's ref opens, so the agent can keep the ref in drive_context
    # the gone item, `<drive id>:<item id>` ("" when unknown): hits of two sources with the same key are one entry
    # (`merge_hits`; DRIVE_FILE_NOTES_RFC §0.3: "an item with both an archived transcript and a note shows both lines")
    item_key: str = ""


def transcript_line(seconds: float, speakers: int, languages: Sequence[str]) -> str:
    """The archived entry's `transcript: 47 min, 2 speakers, uk/en` line, as the media card writes it; "" when the
    ledger snapshot holds no duration (a stub or an unrendered document)."""
    if seconds <= 0:
        return ""
    parts = [f"{max(1, (int(seconds) + 30) // 60)} min"]
    if speakers > 0:
        parts.append(f"{speakers} speaker{'' if speakers == 1 else 's'}")
    if languages:
        parts.append("/".join(languages))
    return "transcript: " + ", ".join(parts)


def matches(concepts: Sequence[Concept], keys: Collection[str]) -> bool:
    """Strict AND, no allowances (the set is small): every concept must meet `keys` (the document's `archive_keys` and
    `name_keys` together) through one of its `word_keys`. With no concept at all (an empty query, which lists the newest) everything matches."""
    held = keys if isinstance(keys, (set, frozenset)) else set(keys)
    return all(any(k in held for k in c.word_keys) for c in concepts)


def passage(stored: StoredTranscript, keys: Iterable[str], max_chars: int = PASSAGE_MAX_CHARS) -> Optional[str]:
    """The first stored segment holding a matched key, with its stamp, cut to `max_chars` (an ellipsis counts). None for
    a stub, or when no segment holds one."""
    transcript = stored.transcript
    if transcript is None:
        return None
    wanted = set(keys)
    if not wanted:
        return None
    labelled = transcript.speakers > 1
    for seg in transcript.segments:
        if seg.gap or not seg.text.strip():
            continue
        if wanted.isdisjoint(word_keys(seg.text)):
            continue
        stamp = f"[{'~' if seg.interpolated else ''}{format_stamp(seg.start_s)}]"
        label = f"Speaker {seg.speaker}: " if labelled and seg.speaker is not None else ""
        text = f"{stamp} {label}{' '.join(seg.text.split())}"
        if len(text) > max_chars:
            text = text[:max(0, max_chars - 1)].rstrip() + "…"
        return text
    return None


def merge_hits(hits: Sequence[DeletedHit]) -> List[DeletedHit]:
    """One entry per gone item (§0.3 of the notes RFC): a later hit whose `item_key` an earlier one holds adds its ref
    lines and its note line to that entry (the earlier hit's head, wrapper ref and drive file stay); hits without a key
    stay as they are. Order kept."""
    merged: List[DeletedHit] = []
    at: Dict[str, int] = {}
    for hit in hits:
        index = at.get(hit.item_key) if hit.item_key else None
        if index is None:
            if hit.item_key:
                at[hit.item_key] = len(merged)
            merged.append(hit)
            continue
        first = merged[index]
        note = "\n".join(x for x in (first.note_line, hit.note_line) if x)
        merged[index] = replace(first, refs=(*first.refs, *hit.refs), note_line=note)
    return merged


def _entry(hit: DeletedHit) -> str:
    if not hit.on_connected_drive:
        status = NOT_CONNECTED_LABEL
    else:
        status = f"{GONE_LABEL}, {hit.day}" if hit.day else GONE_LABEL
        if not hit.verified:
            status += f" ({UNVERIFIED_NOTE})"
    lines: List[str] = [f"{hit.name} | {hit.path} | {status}"]
    for extra in (hit.transcript_line, hit.passage, *hit.refs, hit.note_line):
        if extra:
            lines.append(extra)
    return "\n".join(lines)


def render_deleted(hits: Sequence[DeletedHit], *, more: int = 0, connected: bool,
                   coverage: Sequence[str] = ()) -> str:
    """`Deleted files (N):`, one wrapped entry per hit, then the coverage lines: `more` archived transcripts that
    matched and were not shown, `coverage` the sources' own lines, and (when `connected` is False) that the archive could not be checked."""
    lines: List[str] = [f"Deleted files ({len(hits)}):"]
    if not hits:
        lines.append(NONE_MATCHED_LINE)
    for hit in hits:
        lines.append(wrap_untrusted(_entry(hit), nonce=hit.nonce, ref=hit.ref, path=hit.path))
    if more > 0:
        lines.append(f"{more} more archived transcripts matched.")
    lines.extend(line for line in coverage if line)
    if not connected:
        lines.append(DRIVE_NOT_CONNECTED_LINE)
    return "\n".join(lines)
