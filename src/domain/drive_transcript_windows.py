"""
Window plan, coverage check, time sanity and the content key (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.4-§27.6,
Δ129, Δ135). Pure: no clock, no I/O.
"""
from typing import Optional, Sequence, Tuple

from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_index_params import (
    TRANSCRIBE_COVERAGE_MIN, TRANSCRIBE_COVERAGE_TAIL_S, TRANSCRIBE_OVERLAP_S, TRANSCRIBE_WINDOW_S,
)
from src.domain.drive_transcript import Segment, WindowSpec

BACKWARD_JITTER_S = 5.0     # §27.6 "Time sanity": a start may step back this far
PAST_END_SLACK_S = 10.0     # a start may pass the window length by this much


def plan_windows(duration_s: float, window_s: float = TRANSCRIBE_WINDOW_S,
                 overlap_s: float = TRANSCRIBE_OVERLAP_S) -> Tuple[WindowSpec, ...]:
    """Window k starts at `k * (window_s - overlap_s)`; the last one is short; a file shorter than a window is one
    window; no window has zero length."""
    if overlap_s < 0 or window_s <= overlap_s:
        raise ValueError("overlap must be below the window")
    if duration_s <= 0:
        return ()
    step = window_s - overlap_s
    out = []
    k = 0
    while True:
        start = k * step
        length = min(float(window_s), duration_s - start)
        out.append(WindowSpec(index=k, start_s=float(start), duration_s=float(length)))
        if start + window_s >= duration_s:
            return tuple(out)
        k += 1


def coverage_ok(window: WindowSpec, segments: Sequence[Segment], no_speech: bool) -> bool:
    """Δ129, T-R31: the last segment starts within the window's tolerance of its end, `max((1 - TRANSCRIBE_COVERAGE_MIN)
    x d, TRANSCRIBE_COVERAGE_TAIL_S)`: a tenth of a long window, a minute of a short one (a recording that ends in a
    quiet minute is not a truncated answer). A `no_speech` window is ok; any other needs a segment."""
    if no_speech:
        return True
    if not segments:
        return False
    tolerance = max((1.0 - TRANSCRIBE_COVERAGE_MIN) * window.duration_s, float(TRANSCRIBE_COVERAGE_TAIL_S))
    return max(s.start_s for s in segments) >= window.duration_s - tolerance - 1e-9


def times_ok(window: WindowSpec, segments: Sequence[Segment]) -> bool:
    """§27.6: no start more than 5 s behind the running maximum (a slow drift cannot pass), none past the window length by more than 10 s."""
    high = None
    for seg in segments:
        if seg.start_s > window.duration_s + PAST_END_SLACK_S:
            return False
        if high is not None and seg.start_s < high - BACKWARD_JITTER_S:
            return False
        high = seg.start_s if high is None else max(high, seg.start_s)
    return True


def transcript_content_key(content_identity: Optional[ContentIdentity], identity: str,
                           content_version: str) -> Tuple[str, bool]:
    """(key, shared) of a recording's stored transcript (MULTI_DRIVE_RFC §10): the rendered content identity
    `{family}:{digest}:{size}`, shared by every copy on any connection; without one, `item:{identity}:{content_version}`,
    not shared, which survives a disconnect and reconnect because the item identity does. The stored transcript's
    document id is `transcript_doc_id(user_id, key)` (user-scoped by design). ValueError when the fallback has no
    identity."""
    if content_identity is not None:
        return content_identity.render(), True
    if not identity:
        raise ValueError("a transcript content key without a content identity needs the item identity")
    return f"item:{identity}:{content_version}", False


def record_content_key(rec: DriveIndexRecord) -> Tuple[str, bool]:
    """`transcript_content_key` of a record (its content identity, item identity and content version). There is no
    other hash fallback (Δ135)."""
    return transcript_content_key(rec.content_identity, rec.identity, rec.content_version)

