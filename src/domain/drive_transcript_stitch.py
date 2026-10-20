"""
Stitching windows into one transcript (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.6, Δ107-Δ109). Pure and
deterministic: no clock, no I/O, no logging.

Public interface:
    rebase(window, segments) -> Tuple[Segment, ...]            # window-local starts to the file's clock
    seam_cuts(specs) -> Tuple[float, ...]                       # the midpoint of each adjacent overlap
    cut_seams(specs, per_window) -> Tuple[Segment, ...]         # keep each segment once, in the window it starts in
    align_speakers(windows) -> (segments, seams_uncertain, speaker_count)
    interpolate(segments, specs=()) -> (segments, shares)       # `~` stamps where a segment runs over TRANSCRIBE_MAX_GAP_S

Residual, stated here as the RFC asks: a speaker silent through a whole overlap becomes a new "Speaker N+1" (the count
may over-state), and a seam mapped on fewer than STITCH_MIN_MATCHED_WORDS words is marked uncertain.
"""
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.drive_index_params import STITCH_MIN_MATCHED_WORDS, TRANSCRIBE_MAX_GAP_S
from src.domain.drive_transcript import Segment, WindowSpec, WindowTranscript

MATCH_PROXIMITY_S = 10.0       # two overlap segments are compared only when their starts are this close
INTERPOLATE_TARGET_S = 60.0    # an inserted stamp is placed so that no piece runs much past a minute
_SENTENCE_END = re.compile(r"(?<=[.!?…。])\s+")


def rebase(window: WindowSpec, segments: Sequence[Segment]) -> Tuple[Segment, ...]:
    """`abs = window.start + start`."""
    return tuple(replace(s, start_s=window.start_s + s.start_s) for s in segments)


def seam_cuts(specs: Sequence[WindowSpec]) -> Tuple[float, ...]:
    """One cut per adjacent pair: the middle of their overlap."""
    return tuple((b.start_s + a.start_s + a.duration_s) / 2.0 for a, b in zip(specs, specs[1:]))


def cut_seams(specs: Sequence[WindowSpec], per_window: Sequence[Sequence[Segment]]) -> Tuple[Segment, ...]:
    """Window k keeps the segments that start before its cut with k+1, and from its cut with k-1 on; a segment that
    crosses a cut belongs to the window it starts in. `per_window` is already on the file's clock."""
    cuts = seam_cuts(specs)
    out: List[Segment] = []
    for k, segments in enumerate(per_window):
        low = cuts[k - 1] if k > 0 else None
        high = cuts[k] if k < len(cuts) else None
        for seg in segments:
            if low is not None and seg.start_s < low:
                continue
            if high is not None and seg.start_s >= high:
                continue
            out.append(seg)
    return tuple(out)


def _tokens(text: str) -> Counter:  # type: ignore[type-arg]
    words = ("".join(ch for ch in raw if ch.isalnum()) for raw in text.casefold().split())
    return Counter(w for w in words if w)


def _overlap_segments(segments: Sequence[Segment], start: float, end: float) -> List[Segment]:
    return [s for s in segments if start <= s.start_s < end and s.speaker is not None and not s.gap]


def _match(prev: Sequence[Segment], cur: Sequence[Segment], start: float, end: float) -> Tuple[Dict[int, int], int]:
    """Map labels of `cur` onto labels of `prev`: (cur label -> prev label, matched words)."""
    left = _overlap_segments(prev, start, end)
    right = _overlap_segments(cur, start, end)
    score: Dict[Tuple[int, int], int] = {}
    for sb in right:
        tokens_b = _tokens(sb.text)
        best: Optional[Tuple[int, float, float, Segment]] = None
        for sa in left:
            delta = abs(sa.start_s - sb.start_s)
            if delta > MATCH_PROXIMITY_S:
                continue
            shared = sum((tokens_b & _tokens(sa.text)).values())
            if shared <= 0:
                continue
            key = (-shared, delta, sa.start_s)
            if best is None or key < (-best[0], best[1], best[2]):
                best = (shared, delta, sa.start_s, sa)
        if best is not None and sb.speaker is not None and best[3].speaker is not None:
            pair = (best[3].speaker, sb.speaker)
            score[pair] = score.get(pair, 0) + best[0]
    mapping: Dict[int, int] = {}
    taken = set()
    matched = 0
    for (a, b), words in sorted(score.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])):
        if a in taken or b in mapping:
            continue
        mapping[b] = a
        taken.add(a)
        matched += words
    return mapping, matched


def align_speakers(windows: Sequence[Tuple[WindowSpec, WindowTranscript]]
                   ) -> Tuple[Tuple[Segment, ...], Tuple[bool, ...], int]:
    """Rebase every window, map each window's labels onto the previous window's through the overlap, cut the seams.
    Returns (segments on the file's clock with one speaker numbering, one `uncertain` flag per seam, speaker count).
    A file whose windows all have no speaker numbers keeps `speaker=None` (count 1)."""
    if not windows:
        return (), (), 0
    specs = [spec for spec, _ in windows]
    numbered = any(s.speaker is not None for _, wt in windows for s in wt.segments)

    def prepared(spec: WindowSpec, wt: WindowTranscript) -> List[Segment]:
        out = rebase(spec, wt.segments)
        if numbered:
            out = tuple(s if s.speaker is not None or s.gap else replace(s, speaker=1) for s in out)
        return list(out)

    per_window: List[List[Segment]] = [prepared(*windows[0])]
    used = max((s.speaker for s in per_window[0] if s.speaker is not None), default=0)
    uncertain: List[bool] = []
    for k in range(1, len(windows)):
        cur = prepared(*windows[k])
        start = specs[k].start_s
        end = specs[k - 1].start_s + specs[k - 1].duration_s
        mapping, matched = _match(per_window[k - 1], cur, start, end) if end > start else ({}, 0)
        for label in sorted({s.speaker for s in cur if s.speaker is not None}):
            if label not in mapping:
                used += 1
                mapping[label] = used
        cur = [s if s.speaker is None else replace(s, speaker=mapping[s.speaker]) for s in cur]
        used = max(used, max((s.speaker for s in cur if s.speaker is not None), default=0))
        per_window.append(cur)
        uncertain.append(matched < STITCH_MIN_MATCHED_WORDS)
    segments = cut_seams(specs, per_window)
    labels = {s.speaker for s in segments if s.speaker is not None}
    count = len(labels) if labels else (1 if segments else 0)
    return segments, tuple(uncertain), count


def _split(seg: Segment, span: float) -> List[Segment]:
    sentences = [p for p in _SENTENCE_END.split(seg.text.strip()) if p]
    if len(sentences) < 2:
        return [seg]
    offsets = [0]
    for sentence in sentences:
        offsets.append(offsets[-1] + len(sentence) + 1)
    total = offsets[-1]

    def at(i: int) -> float:
        return seg.start_s + span * offsets[i] / total

    pieces: List[Tuple[int, int]] = []      # [first sentence, last sentence + 1)
    first = 0
    for i in range(1, len(sentences)):
        if at(i + 1) - at(first) > INTERPOLATE_TARGET_S:
            pieces.append((first, i))
            first = i
    pieces.append((first, len(sentences)))
    out = []
    for n, (a, b) in enumerate(pieces):
        text = " ".join(sentences[a:b])
        if n == 0:
            out.append(replace(seg, text=text))
        else:
            out.append(Segment(start_s=at(a), speaker=seg.speaker, text=text, interpolated=True))
    return out


def interpolate(segments: Sequence[Segment], specs: Sequence[WindowSpec] = ()
                ) -> Tuple[Tuple[Segment, ...], Tuple[float, ...]]:
    """Where a segment runs longer than TRANSCRIBE_MAX_GAP_S before the next segment starts (for the last one, before
    the end of the last window when `specs` is given), split it at sentence boundaries, placing the stamps by
    character proportion and marking them interpolated. Returns the segments and, per window of `specs`, the share of
    that window's segments (by seam cut) whose stamp is interpolated.

    The span is measured to the next segment of any speaker, not of the same speaker: a short reply followed by a long
    turn of someone else is not a long silence of the first speaker."""
    end_s = specs[-1].start_s + specs[-1].duration_s if specs else None
    out: List[Segment] = []
    for i, seg in enumerate(segments):
        nxt = segments[i + 1].start_s if i + 1 < len(segments) else end_s
        span = (nxt - seg.start_s) if nxt is not None else 0.0
        if seg.gap or span <= TRANSCRIBE_MAX_GAP_S:
            out.append(seg)
        else:
            out.extend(_split(seg, span))
    if not specs:
        return tuple(out), ()
    cuts = seam_cuts(specs)
    totals = [0] * len(specs)
    marked = [0] * len(specs)
    for seg in out:
        k = bisect_right(cuts, seg.start_s)
        totals[k] += 1
        marked[k] += 1 if seg.interpolated else 0
    return tuple(out), tuple(m / t if t else 0.0 for m, t in zip(marked, totals))
