"""
Drive transcript mirror — the file a transcript is written as, and the keys an archived one is found by
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.10, §27.9.6; Δ198, Δ213). Pure: no I/O, no clock, no logging.

The file is Markdown, UTF-8, LF line ends:

    # <source name>
    <source path> · [<open the recording>](<web_url>)
    <recorded on> · <duration> · <N speakers> · <languages>
    <the managed line>

    ---

    <open_text, uncapped: the possibly-incomplete note, the uncertain-seam line, then the body>

A stub is the header and one localized sentence. The header's numbers come from the RECORD (the fields the mirror digest
hashes), the body from the stored transcript. The UI language is a rendering input, never a digest input: a language
change re-renders nothing until a file is rewritten for another reason (Δ120).

Public interface:
    RENDER_VERSION
    MirrorStrings                                          # the localized strings, supplied by the caller
    render_file(stored, rec, strings, *, source_path, recorded_on) -> bytes
    rendered_hash(data) -> str
    readme_bytes(strings) -> bytes
    archive_word_keys(stored) -> Tuple[str, ...]
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .drive_index import DriveIndexRecord
from .drive_index_params import ARCHIVE_KEYS_MAX
from .drive_keys import word_keys
from .drive_transcript import StoredTranscript, TranscriptStub
from .drive_transcript_render import open_text
from .drive_transcript_time import format_stamp

RENDER_VERSION = 1

_ESCAPED = "\\`*_{}[]()<>#|~"
_SEPARATOR = " · "


@dataclass(frozen=True)
class MirrorStrings:
    """The localized text of the file and the read-me. Sentences may use `{duration}` (`MM:SS` or `H:MM:SS`, or the
    empty string when unknown); counts use `{n}`. The caller picks the language (the document's `render_language`)."""
    open_recording: str                 # the link text: "open the recording"
    managed_line: str                   # "Managed by the bot: this file may be rewritten. Automatic transcript, may contain errors."
    duration_minutes: str               # "{n} min"
    speakers_one: str                   # "{n} speaker"
    speakers_many: str                  # "{n} speakers"
    stub_no_speech: str
    stub_too_long: str
    stub_too_large: str
    stub_no_audio: str
    readme: str                         # the whole `_README.md` text; it must say transcripts of deleted files are in Archive/


def _escape(text: str) -> str:
    """Markdown control characters escaped, line breaks folded to spaces (a heading and a path are one line)."""
    flat = " ".join(text.replace("\r", "\n").split("\n"))
    return "".join("\\" + ch if ch in _ESCAPED else ch for ch in flat)


def _duration(seconds: int, strings: MirrorStrings) -> str:
    if seconds <= 0:
        return ""
    return strings.duration_minutes.format(n=max(1, (seconds + 30) // 60))


def _speakers(count: int, strings: MirrorStrings) -> str:
    if count <= 0:
        return ""
    return (strings.speakers_one if count == 1 else strings.speakers_many).format(n=count)


def _header(rec: DriveIndexRecord, strings: MirrorStrings, source_path: str, recorded_on: str) -> List[str]:
    lines = [f"# {_escape(rec.name)}"]
    where = _escape(source_path)
    if rec.web_url:
        link = f"[{_escape(strings.open_recording)}]({rec.web_url.replace(' ', '%20').replace(')', '%29')})"
        where = f"{where}{_SEPARATOR}{link}" if where else link
    if where:
        lines.append(where)
    facts = [recorded_on, _duration(rec.transcript_seconds, strings), _speakers(rec.transcript_speakers, strings),
             "/".join(rec.transcript_languages)]
    line = _SEPARATOR.join(f for f in facts if f)
    if line:
        lines.append(line)
    lines.append(strings.managed_line)
    return lines


def _stub_sentence(stub: str, seconds: int, strings: MirrorStrings) -> str:
    sentences: Dict[str, str] = {
        TranscriptStub.NO_SPEECH.value: strings.stub_no_speech,
        TranscriptStub.TOO_LONG.value: strings.stub_too_long,
        TranscriptStub.TOO_LARGE.value: strings.stub_too_large,
        TranscriptStub.NO_AUDIO.value: strings.stub_no_audio,
    }
    duration = format_stamp(seconds) if seconds > 0 else ""
    return sentences.get(stub, "").format(duration=duration)


def render_file(stored: StoredTranscript, rec: DriveIndexRecord, strings: MirrorStrings, *, source_path: str,
                recorded_on: str) -> bytes:
    """The mirror file for `rec` (§27.9.10). `recorded_on` is already formatted by the caller (it is localized).
    The body is `open_text` with no cap, exactly what Smart opens (the cut note never appears)."""
    lines = _header(rec, strings, source_path, recorded_on)
    if stored.transcript is None:
        body = _stub_sentence(stored.stub or rec.transcript_stub, rec.transcript_seconds, strings)
    else:
        body = open_text(stored, max_chars=sys.maxsize)
    text = "\n".join(lines) + "\n\n---\n\n" + body + "\n"
    return text.encode("utf-8")


def rendered_hash(data: bytes) -> str:
    """SHA-256 of the bytes, hex: a replace happens only when it differs (§27.9.10)."""
    return hashlib.sha256(data).hexdigest()


def readme_bytes(strings: MirrorStrings) -> bytes:
    """`_README.md`: the localized read-me, UTF-8, LF, one trailing newline."""
    return (strings.readme.strip("\n") + "\n").encode("utf-8")


def archive_word_keys(stored: StoredTranscript) -> Tuple[str, ...]:
    """`archive_keys` of a ledger document (Δ213): the union of `word_keys` over each stored SEGMENT (each far below
    the per-chunk cap), de-duplicated in first-seen order, at most ARCHIVE_KEYS_MAX. A stub has none."""
    transcript = stored.transcript
    if transcript is None:
        return ()
    seen: Dict[str, None] = {}
    for seg in transcript.segments:
        if seg.gap or not seg.text.strip():
            continue
        for key in word_keys(seg.text):
            if key not in seen:
                seen[key] = None
                if len(seen) >= ARCHIVE_KEYS_MAX:
                    return tuple(seen)
    return tuple(seen)
