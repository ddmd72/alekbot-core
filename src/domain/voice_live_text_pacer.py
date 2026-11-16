"""LiveTextPacer — the live call text of one mode call, released in step with the played audio
(VOICE_CALL_LIVE_TEXT_RFC §4.2–§4.4).

Why: the provider streams Lelik's transcript 11–15x faster than the audio plays (RFC §2), so text shown on arrival
would print a whole reply at once, including words a barge-in drops before they are heard. The pacer holds every
phrase of Lelik's reply until the audio it belongs to has been played, and keeps the caller's lines in the order the
turns happened. `VoiceSessionService` feeds it the provider's events and the playback counters and sends the frames it
returns on the control channel (`src/domain/voice_control_protocol.py`, `line` and `snapshot`).

Lines. Ids are `l1`, `l2` ... in allocation order; provider ids never leave the pacer. A caller line is allocated when
the caller's turn ends (`caller_turn`), a Lelik line when a reply starts (`reply_started`), so the caller's line sits
above the reply it caused however late its transcript arrives. A caller line is not paced: it is already said, and its
text goes out (`set`) at the next tick. A Lelik line grows by `append` frames and closes with one `end`.

Anchors. Each Lelik phrase is released once `played_bytes` reaches its anchor, a byte offset in the call's outbound
audio stream (the `PlaybackTracker` counters):
- with the provider's offset (xAI `start_time`): the start byte of the phrase's item (`reply_audio`) plus
  `offset_ms x bytes_per_ms`; a delta without an item id uses the line's first item. A phrase that arrives before its
  item's first audio (xAI can send it first) is held, and anchored when that audio arrives;
- without an offset: `sent_bytes` when the phrase arrived, so the text leads by at most the audio queued before it.
`lag_ms` delays every release by that much audio (`LIVE_TEXT_LAG_MS`, the ear's lag behind the SFU).

The cut. On barge-in the service calls `cut(played_bytes)` before the outbound audio is cleared: what was played is
released, everything else of the open replies is dropped, and those lines stay closed to every later delta.

Snapshot. `snapshot()` is what the page holds after every frame sent so far (text queued for the next tick is not in
it), so a (re)connecting page gets the snapshot and then the live frames, with no line doubled.

Pure: no I/O, no clock, no logging. Text is never logged by anyone: `stats` gives counts only.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from src.domain.voice_control_protocol import (
    KIND_ASK,
    LINE_APPEND,
    LINE_DONE,
    LINE_END,
    LINE_INTERRUPTED,
    LINE_SET,
    WHO_CALLER,
    WHO_LELIK,
    encode,
    line_frame,
    snapshot_frame,
)

DEFAULT_MAX_LINES = 50                # RFC §4.7: a snapshot holds the last 50 lines
DEFAULT_MAX_SNAPSHOT_BYTES = 20_000   # RFC §4.7: a snapshot frame is at most 20 KB


class LiveTextPacer:
    """The live text of one call: line ids, paced release of Lelik's phrases, the barge-in cut and the snapshot."""

    def __init__(self, bytes_per_ms: int, lag_ms: int = 0, max_lines: int = DEFAULT_MAX_LINES,
                 max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES) -> None:
        """`bytes_per_ms`: the outbound audio format's bytes per millisecond (`PlaybackTracker.bytes_per_ms`)."""
        self._bytes_per_ms = bytes_per_ms
        self._lag_bytes = max(lag_ms, 0) * bytes_per_ms
        self._max_lines = max_lines
        self._max_snapshot_bytes = max_snapshot_bytes
        self._lines: Dict[int, _Line] = {}      # kept lines by number, oldest first
        self._item_line: Dict[str, int] = {}    # provider item id -> line number (kept or forgotten)
        self._next_number = 1
        self._latest_lelik: Optional[int] = None
        self._seq = 0
        self._stats = {"lines": 0, "phrases": 0, "phrases_with_offset": 0, "released": 0, "dropped": 0}

    def caller_turn(self, item_id: Optional[str], *, ask: bool = False) -> None:
        """The caller's turn ended (`speech_stopped`; `turn_committed` as a fallback): allocate its line. Idempotent
        per non-None item id. `ask`: the turn is the question of an "Ask Lelik" exchange."""
        if item_id is not None and self._caller_line(item_id) is not None:
            return
        self._allocate(WHO_CALLER, ask, item_id)

    def caller_text(self, item_id: Optional[str], text: str, *, ask: bool = False) -> None:
        """The caller's transcript (`user_transcript`): queue `set` for the next tick. Blank text removes a line the
        page has not seen yet and sends nothing. `item_id=None` fills the oldest caller line still waiting for text.
        A transcript with no line allocates one now, with `ask`."""
        line = self._oldest_waiting_caller_line() if item_id is None else self._caller_line(item_id)
        if not text.strip():
            if line is not None and not line.shown_text:
                del self._lines[line.number]
            return
        if line is None:
            line = self._allocate(WHO_CALLER, ask, item_id)
        line.out_set = text

    def reply_started(self, *, ask: bool = False) -> None:
        """A reply started (`response_created`): allocate a new Lelik line. `ask`: the reply answers an "Ask Lelik"
        question."""
        self._latest_lelik = self._allocate(WHO_LELIK, ask, None).number

    def reply_audio(self, item_id: Optional[str], start_bytes: int) -> None:
        """The first audio of a reply item arrived; `start_bytes` is `sent_bytes` where it starts. Anchors the
        phrases held for that item."""
        line = self._lelik_line_for(item_id)
        if line is None:
            return
        if item_id is not None:
            line.item_starts[item_id] = start_bytes
        if line.first_start is None:
            line.first_start = start_bytes
        if line.closed:
            return
        for phrase in line.phrases:
            if phrase.anchor is None and phrase.offset_ms is not None:
                base = line.base(phrase.item_id)
                if base is not None:
                    phrase.anchor = base + phrase.offset_ms * self._bytes_per_ms

    def reply_delta(self, item_id: Optional[str], text: str, offset_ms: Optional[int], sent_bytes: int) -> None:
        """One phrase of Lelik's transcript (`model_transcript_delta`). `offset_ms`: where it starts in its item's
        audio, None when the provider gives none; `sent_bytes`: the outbound counter now. Empty text is ignored."""
        if not text:
            return
        self._stats["phrases"] += 1
        if offset_ms is not None:
            self._stats["phrases_with_offset"] += 1
        line = self._lelik_line_for(item_id)
        if line is None or line.closed:
            self._stats["dropped"] += 1
            return
        phrase = _Phrase(text=text, item_id=item_id, offset_ms=offset_ms)
        if offset_ms is None:
            phrase.anchor = sent_bytes
        else:
            base = line.base(item_id)
            if base is not None:
                phrase.anchor = base + offset_ms * self._bytes_per_ms
        line.phrases.append(phrase)

    def reply_done(self) -> None:
        """The reply ended (`response_done`): the latest Lelik line ends `done` once all its text is released. A line
        already closed is not touched."""
        line = self._kept(self._latest_lelik)
        if line is not None and not line.closed:
            line.reply_done = True

    def cut(self, played_bytes: int) -> None:
        """Barge-in (called BEFORE the outbound audio is cleared) or teardown: every open Lelik line releases what was
        played (the lag ignored: that audio was heard), drops the rest and closes. It ends `interrupted` when anything
        was dropped or the reply had not ended, else `done`; a line with no text released is dropped silently. The
        frames go out at the next `tick`."""
        for line in list(self._lines.values()):
            if line.who != WHO_LELIK or line.closed:
                continue
            dropped = False
            for phrase in line.phrases:
                if phrase.anchor is not None and phrase.anchor <= played_bytes:
                    self._release(line, phrase)
                else:
                    self._stats["dropped"] += 1
                    dropped = True
            line.phrases = []
            line.closed = True
            if not line.released:
                del self._lines[line.number]
                continue
            line.out_end = LINE_INTERRUPTED if dropped or not line.reply_done else LINE_DONE

    def tick(self, played_bytes: int) -> List[Dict[str, Any]]:
        """The frames to send now: per line (in id order) one `set` or `append` with everything released since the
        last tick, joined verbatim, then its `end` when the line has finished. Each frame takes the next `seq`."""
        threshold = played_bytes - self._lag_bytes
        frames: List[Dict[str, Any]] = []
        for line in list(self._lines.values()):
            if line.who == WHO_LELIK and not line.closed and not self._advance(line, threshold):
                continue
            frames.extend(self._flush(line))
        return frames

    def snapshot(self) -> Dict[str, Any]:
        """A `snapshot` frame (a fresh `seq`) of what the page holds now: the kept lines that have text, oldest first,
        trimmed to `max_snapshot_bytes` by dropping the oldest lines, and the end of the one left if it alone is over."""
        entries = [line.snapshot_entry() for line in self._lines.values() if line.shown_text]
        seq = self._next_seq()
        frame = snapshot_frame(seq, entries)
        while len(entries) > 1 and self._over_cap(frame):
            entries.pop(0)
            frame = snapshot_frame(seq, entries)
        if entries and self._over_cap(frame):
            frame = self._trimmed(seq, entries[0])
        return frame

    @property
    def stats(self) -> Mapping[str, int]:
        """Counts for one log line, never text: lines allocated, phrases received (and with an offset), phrases
        released and dropped."""
        return dict(self._stats)

    # ------------------------------------------------------------------ internals

    def _allocate(self, who: str, ask: bool, item_id: Optional[str]) -> "_Line":
        line = _Line(number=self._next_number, who=who, kind=KIND_ASK if ask else None)
        self._next_number += 1
        self._lines[line.number] = line
        if item_id is not None:
            self._item_line[item_id] = line.number
        self._stats["lines"] += 1
        self._forget_old()
        return line

    def _forget_old(self) -> None:
        """Keeps the last `max_lines` lines. Item ids of forgotten lines stay mapped a while longer, so their late
        events are dropped instead of landing on a newer line."""
        while len(self._lines) > self._max_lines:
            oldest = self._lines.pop(next(iter(self._lines)))
            self._stats["dropped"] += len(oldest.phrases)
        if self._lines and len(self._item_line) > 4 * self._max_lines:
            floor = next(iter(self._lines)) - self._max_lines
            self._item_line = {item: number for item, number in self._item_line.items() if number >= floor}

    def _kept(self, number: Optional[int]) -> Optional["_Line"]:
        return self._lines.get(number) if number is not None else None

    def _caller_line(self, item_id: str) -> Optional["_Line"]:
        line = self._kept(self._item_line.get(item_id))
        return line if line is not None and line.who == WHO_CALLER else None

    def _oldest_waiting_caller_line(self) -> Optional["_Line"]:
        for line in self._lines.values():
            if line.who == WHO_CALLER and line.out_set is None and not line.shown_text:
                return line
        return None

    def _lelik_line_for(self, item_id: Optional[str]) -> Optional["_Line"]:
        """The line an item's events go to: the one bound to the item, else the latest Lelik line, bound to it now.
        None when the item's line is forgotten or no Lelik line exists."""
        if item_id is not None and item_id in self._item_line:
            line = self._kept(self._item_line[item_id])
            return line if line is not None and line.who == WHO_LELIK else None
        line = self._kept(self._latest_lelik)
        if line is not None and item_id is not None:
            self._item_line[item_id] = line.number
        return line

    def _advance(self, line: "_Line", threshold: int) -> bool:
        """Releases what has played on an open Lelik line and closes it once the reply ended and nothing is left.
        False when the line was dropped (it ended with nothing ever released)."""
        if line.reply_done:
            held = [phrase for phrase in line.phrases if phrase.anchor is None]
            self._stats["dropped"] += len(held)
        remaining: List[_Phrase] = []
        for phrase in line.phrases:
            if phrase.anchor is not None and phrase.anchor <= threshold:
                self._release(line, phrase)
            elif phrase.anchor is not None or not line.reply_done:
                remaining.append(phrase)
        line.phrases = remaining
        if line.reply_done and not remaining:
            line.closed = True
            if not line.released:
                del self._lines[line.number]
                return False
            line.out_end = LINE_DONE
        return True

    def _release(self, line: "_Line", phrase: "_Phrase") -> None:
        line.out_text += phrase.text
        line.released = True
        self._stats["released"] += 1

    def _flush(self, line: "_Line") -> List[Dict[str, Any]]:
        frames: List[Dict[str, Any]] = []
        if line.out_set is not None:
            frames.append(line_frame(self._next_seq(), line.name, LINE_SET, who=line.who, text=line.out_set,
                                     kind=line.kind))
            line.shown_text, line.out_set = line.out_set, None
        if line.out_text:
            frames.append(line_frame(self._next_seq(), line.name, LINE_APPEND, who=line.who, text=line.out_text,
                                     kind=line.kind))
            line.shown_text += line.out_text
            line.out_text = ""
        if line.out_end is not None:
            frames.append(line_frame(self._next_seq(), line.name, LINE_END, status=line.out_end))
            line.shown_status, line.out_end = line.out_end, None
        return frames

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _over_cap(self, frame: Mapping[str, Any]) -> bool:
        return len(encode(frame).encode("utf-8")) > self._max_snapshot_bytes

    def _trimmed(self, seq: int, entry: Dict[str, Any]) -> Dict[str, Any]:
        """The snapshot of one line whose text alone is over the cap: the longest end of its text that fits (no line
        when not even an empty text fits)."""
        text = str(entry["text"])

        def frame_keeping(chars: int) -> Dict[str, Any]:
            return snapshot_frame(seq, [{**entry, "text": text[len(text) - chars:]}])

        if self._over_cap(frame_keeping(0)):
            return snapshot_frame(seq, [])
        low, high = 0, len(text)  # frame_keeping(low) fits; find the largest that does
        while low < high:
            middle = (low + high + 1) // 2
            if self._over_cap(frame_keeping(middle)):
                high = middle - 1
            else:
                low = middle
        return frame_keeping(low)


@dataclass
class _Phrase:
    text: str
    item_id: Optional[str]
    offset_ms: Optional[int]
    anchor: Optional[int] = None  # None while held: its item's audio has not arrived


@dataclass
class _Line:
    number: int
    who: str
    kind: Optional[str]
    # Lelik lines
    phrases: List[_Phrase] = field(default_factory=list)  # received, not released, in arrival order
    item_starts: Dict[str, int] = field(default_factory=dict)
    first_start: Optional[int] = None
    reply_done: bool = False
    closed: bool = False     # takes no more deltas (cut, or ended)
    released: bool = False   # some text was released (sent or queued)
    # Queued for the next tick
    out_set: Optional[str] = None
    out_text: str = ""
    out_end: Optional[str] = None
    # What the page holds after every frame sent
    shown_text: str = ""
    shown_status: Optional[str] = None

    @property
    def name(self) -> str:
        return f"l{self.number}"

    def base(self, item_id: Optional[str]) -> Optional[int]:
        """Where the audio a phrase's offset counts from starts: its item's first byte, or the line's first item's
        when the phrase names no item. None while that audio has not arrived."""
        return self.item_starts.get(item_id) if item_id is not None else self.first_start

    def snapshot_entry(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"line": self.name, "who": self.who, "text": self.shown_text}
        if self.shown_status is not None:
            entry["status"] = self.shown_status
        if self.kind is not None:
            entry["kind"] = self.kind
        return entry
