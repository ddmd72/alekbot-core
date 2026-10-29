"""
What a transcript looks like to the index, to the card and to Smart's open (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§27.8 a, §27.11; plan G17-G19). Pure: no clock, no I/O, no logging.

Public interface:
    TRANSCRIPT_BODY_VERSION                                    # versions `render_body`, the indexed layout (G18)
    render_body(transcript) -> str                             # one paragraph per segment, a stamp at every head
    card_segment(rec) -> Optional[str]                         # `transcript: 47 min, 2 speakers, uk/en` or a stub line
    open_needs_cut(stored, *, max_chars) -> bool               # whether open_text would cut
    open_text(stored, *, max_chars, mirror_path, live, indexed) -> str   # what opening a recording returns, capped (G19, §29.10)
    cap_plain_text(text, *, max_chars, path) -> str            # a transcript file read as text, cut the same way

The labels are language-neutral (G17): `Speaker N`, `[MM:SS]`, `[~MM:SS]`. The localized header, stubs, gap sentence
and summary belong to the mirror file, not here. Every stub has this deterministic structural rendering.
"""
from typing import Callable, List, Optional

from src.domain.drive_index import DriveIndexRecord, RecordState
from src.domain.drive_enrichment import JobState
from src.domain.drive_transcript import StitchedTranscript, StoredTranscript, TranscriptStub
from src.domain.drive_open_cut import cut_line, head_at_paragraph
from src.domain.drive_open_bound_text import BoundText
from src.domain.drive_open_texts import TRANSCRIPT_SUBJECT
from src.domain.drive_text_extent import ExtentState, ExtentWhy, TextExtent
from src.domain.drive_transcript_time import format_stamp

TRANSCRIPT_BODY_VERSION = 1

TRUNCATED_NOTE = "Note: part of this recording may be missing from the transcript."
GAP_TEXT = "[this part could not be transcribed]"
UNCERTAIN_SEAM_LINE = "[speaker labels may differ between parts of this recording]"
_STUB_TEXT = {
    TranscriptStub.NO_SPEECH.value: "no speech detected",
    TranscriptStub.TOO_LONG.value: "too long",
    TranscriptStub.TOO_LARGE.value: "too large",
    TranscriptStub.NO_AUDIO.value: "no audio track",
}


def render_body(transcript: StitchedTranscript) -> str:
    """One paragraph per segment, separated by one blank line so the chunker's paragraph split puts a stamp at every
    chunk head. A single speaker carries no label."""
    labelled = transcript.speakers > 1
    paragraphs: List[str] = []
    for seg in transcript.segments:
        stamp = f"[{'~' if seg.interpolated else ''}{format_stamp(seg.start_s)}]"
        if seg.gap:
            paragraphs.append(f"{stamp} {GAP_TEXT}")
            continue
        text = " ".join(seg.text.split())
        if not text:
            continue
        label = f"Speaker {seg.speaker}: " if labelled and seg.speaker is not None else ""
        paragraphs.append(f"{stamp} {label}{text}")
    return "\n\n".join(paragraphs)


def _stub_line(stub: str, seconds: int = 0) -> Optional[str]:
    text = _STUB_TEXT.get(stub)
    if text is None:
        return None
    if stub == TranscriptStub.TOO_LONG.value and seconds > 0:
        text = f"{text}: {format_stamp(seconds)}"
    return f"transcript: none ({text})"


def card_segment(rec: DriveIndexRecord) -> Optional[str]:
    """The transcript line of a media card; None while there is nothing to say (no transcript, a failed one, or a
    record whose index run failed: no chunks back a "transcript: N min" line, T-R24)."""
    if rec.transcript_state != JobState.CURRENT.value or rec.state is RecordState.FAILED:
        return None
    if rec.transcript_stub:
        return _stub_line(rec.transcript_stub, rec.transcript_seconds)
    if rec.transcript_seconds <= 0:
        return None
    parts = [f"{max(1, (rec.transcript_seconds + 30) // 60)} min"]
    if rec.transcript_speakers > 0:
        parts.append(f"{rec.transcript_speakers} speaker{'' if rec.transcript_speakers == 1 else 's'}")
    if rec.transcript_languages:
        parts.append("/".join(rec.transcript_languages))
    if rec.transcript_truncated:
        parts.append("may be incomplete")
    return "transcript: " + ", ".join(parts)


def _head(transcript: StitchedTranscript) -> str:
    return (TRUNCATED_NOTE + "\n\n" if transcript.truncated else "") \
        + (UNCERTAIN_SEAM_LINE + "\n\n" if transcript.uncertain_seam else "")


def _archived_note(pointer: str) -> Callable[[int, int], str]:
    def note(shown: int, total: int) -> str:
        return (f"[Transcript cut at {shown} of {total} characters.{pointer} "
                f"Search for a passage to read a specific part.]")
    return note


def _live_note(pointer: str, indexed: bool) -> Callable[[int, int], str]:
    """RFC §29.7 cut line for a live transcript: the third form when the index can page it, else the first."""
    extent = TextExtent(ExtentState.PAGEABLE) if indexed else TextExtent(ExtentState.NOT_INDEXED, ExtentWhy.NO_TEXT)

    def note(shown: int, total: int) -> str:
        return cut_line(TRANSCRIPT_SUBJECT, BoundText("", total, shown, extent.state), extent) + pointer
    return note


def _cut(full: str, head: str, max_chars: int, note: Callable[[int, int], str]) -> str:
    """`head` + `full` cut at a paragraph boundary so the whole result, the cut note included, stays within
    `max_chars` (G19). `note(shown, total)` writes the cut note."""
    room = max(0, max_chars - len(head) - len(note(len(full), len(full))) - 2)
    body = head_at_paragraph(full, room)
    return head + body + "\n\n" + note(len(body), len(full))


def _pointer(path: str) -> str:
    return f" The full text is in {path}." if path else ""


def open_needs_cut(stored: StoredTranscript, *, max_chars: int) -> bool:
    """True when `open_text` would cut this transcript at `max_chars` (so the caller looks up the file path only then)."""
    transcript = stored.transcript
    if transcript is None:
        return False
    return len(_head(transcript)) + len(render_body(transcript)) > max_chars


def open_text(stored: StoredTranscript, *, max_chars: int, mirror_path: str = "", live: bool = False,
              indexed: bool = False) -> str:
    """The transcript as Smart reads it: the indexed body, cut at a paragraph boundary so the whole result (seam line
    and cut note included) stays within `max_chars` (G19). The note points at `mirror_path` when the caller has one.
    A stub is its card line; nothing stored is the empty string. `live=True` (RFC §29.10) writes the open bound's cut
    line instead of the archived note; `indexed` says the index can page the record (the third cut form)."""
    transcript = stored.transcript
    if transcript is None:
        return _stub_line(stored.stub) or ""
    full = render_body(transcript)
    head = _head(transcript)
    if len(head) + len(full) <= max_chars:
        return head + full
    note = _live_note(_pointer(mirror_path), indexed) if live else _archived_note(_pointer(mirror_path))
    return _cut(full, head, max_chars, note)


def cap_plain_text(text: str, *, max_chars: int, path: str = "") -> str:
    """A transcript file read as plain text (an archived transcript with no store entry, §27.9.11), cut the way
    `open_text` cuts: at a paragraph boundary, the cut note inside `max_chars` and naming the file's `path`."""
    if len(text) <= max_chars:
        return text
    return _cut(text, "", max_chars, _archived_note(_pointer(path)))
