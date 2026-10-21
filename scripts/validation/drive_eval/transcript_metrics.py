"""
Transcript eval metrics (USER_DRIVE_INDEXING_RFC §27.14 "Eval"): pure functions over a clip's gold and its stitched
transcript. No network, no clock, no model. The owner-run live part is `transcript_eval.py`.

Metrics per clip:
  wer                      word error rate of the whole transcript against the scripted lines
  speaker_agreement        share of matched lines whose speaker agrees under the best label mapping (labels are arbitrary)
  speaker_overcount        speakers the transcript names beyond the scripted ones (never below 0)
  timestamp_error_s        |transcribed start - intended start| at each probe line: mean, p90 and max
  seam_duplicates/losses   scripted lines found more than once / not found, over the whole clip and within SEAM_ZONE_S of a
                           window cut (the seam cut and the stitching are what this measures)
  interpolated_share       share of segments whose stamp the code computed (the `~` stamps)
  coverage_resubmits       windows the coverage or time checks sent back (counted by the live driver)

A scripted line is FOUND by a segment when at least LINE_FOUND_SHARE of the line's words occur in it (multiset), so a model
that merges two lines in one segment still finds both and one that splits a line finds it in the larger piece. Ukrainian is
reported on its own: `by_language` groups clips by the language of their gold, "uk" never mixed into the others.

Bars (§24.11 practice): `make_bars` writes what a first live run measured, floored or ceilinged to six places so a bar never
passes a run that did not earn it; nothing is invented. `regressions` names every metric worse than its bar.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.drive_transcript import Segment, StitchedTranscript, WindowSpec
from src.domain.drive_transcript_stitch import seam_cuts

LINE_FOUND_SHARE = 0.5
SEAM_ZONE_S = 120.0
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# direction of each aggregate metric: True = higher is better (bars floor), False = lower is better (bars ceil)
HIGHER_IS_BETTER = {"speaker_agreement": True, "wer": False, "speaker_overcount": False, "timestamp_error_mean_s": False,
                    "timestamp_error_p90_s": False, "seam_duplicates": False, "seam_losses": False,
                    "interpolated_share": False, "coverage_resubmits": False}


def words(text: str) -> List[str]:
    return _WORD.findall(text.lower())


def wer(reference: str, hypothesis: str) -> float:
    """Word edit distance over the reference length (insertions count, so it can exceed 1)."""
    ref, hyp = words(reference), words(hypothesis)
    if not ref:
        return 0.0 if not hyp else float(len(hyp))
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        current = [i]
        for j, h in enumerate(hyp, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return previous[-1] / len(ref)


def _containment(line: Counter, segment: Counter) -> float:
    total = sum(line.values())
    return sum((line & segment).values()) / total if total else 0.0


def match_lines(gold_lines: Sequence[Mapping[str, Any]],
                segments: Sequence[Segment]) -> List[Tuple[Optional[int], int]]:
    """Per gold line: (the index of its nearest-in-time finding segment or None, how many MORE segments find it)."""
    seg_words = [Counter(words(s.text)) for s in segments]
    out: List[Tuple[Optional[int], int]] = []
    for line in gold_lines:
        want = Counter(words(str(line["text"])))
        found = [i for i, sw in enumerate(seg_words)
                 if not segments[i].gap and _containment(want, sw) >= LINE_FOUND_SHARE]
        if not found:
            out.append((None, 0))
            continue
        primary = min(found, key=lambda i: abs(segments[i].start_s - float(line["start_s"])))
        out.append((primary, len(found) - 1))
    return out


def speaker_agreement(gold_lines: Sequence[Mapping[str, Any]], segments: Sequence[Segment],
                      matches: Sequence[Tuple[Optional[int], int]]) -> Optional[float]:
    """Each transcript label is mapped to the scripted speaker it most often co-occurs with; the score is the share of
    matched lines that agree. None when no matched segment carries a label (a single-speaker or unlabelled answer)."""
    pairs = [(segments[m].speaker, int(g["speaker"])) for g, (m, _) in zip(gold_lines, matches)
             if m is not None and segments[m].speaker is not None]
    if not pairs:
        return None
    votes: Dict[int, Counter] = {}
    for hyp, gold in pairs:
        votes.setdefault(hyp, Counter())[gold] += 1
    mapping = {hyp: counter.most_common(1)[0][0] for hyp, counter in votes.items()}
    return sum(1 for hyp, gold in pairs if mapping[hyp] == gold) / len(pairs)


def timestamp_errors(gold_lines: Sequence[Mapping[str, Any]], segments: Sequence[Segment],
                     matches: Sequence[Tuple[Optional[int], int]], probes: Sequence[float]) -> List[float]:
    """|start - intended start| for each probe whose line was found; a probe not found is a loss, not an error."""
    probe_set = set(probes)
    return [abs(segments[m].start_s - float(g["start_s"])) for g, (m, _) in zip(gold_lines, matches)
            if m is not None and float(g["start_s"]) in probe_set]


def _p90(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]


def clip_metrics(gold_clip: Mapping[str, Any], stitched: StitchedTranscript, *,
                 window_specs: Sequence[WindowSpec] = (), coverage_resubmits: int = 0) -> Dict[str, Any]:
    """Every metric of one clip; keys absent for a clip they do not apply to (a no-speech clip has no WER)."""
    lines = gold_clip["lines"]
    segments = stitched.segments
    out: Dict[str, Any] = {"id": gold_clip["id"], "family": gold_clip["family"], "language": gold_clip["language"],
                           "coverage_resubmits": coverage_resubmits, "truncated": stitched.truncated,
                           "segments": len(segments)}
    if gold_clip.get("synth"):     # tone or music: the right answer is no transcript at all
        out["no_speech_ok"] = not any(s.text.strip() for s in segments if not s.gap)
        return out
    spoken = " ".join(s.text for s in segments if not s.gap)
    out["wer"] = wer(" ".join(str(ln["text"]) for ln in lines), spoken)
    matches = match_lines(lines, segments)
    agreement = speaker_agreement(lines, segments, matches)
    if agreement is not None:
        out["speaker_agreement"] = agreement
    out["speaker_overcount"] = max(0, stitched.speakers - int(gold_clip["speakers"]))
    errors = timestamp_errors(lines, segments, matches, gold_clip.get("probes", ()))
    if errors:
        out["timestamp_error_mean_s"] = sum(errors) / len(errors)
        out["timestamp_error_p90_s"] = _p90(errors)
        out["timestamp_error_max_s"] = max(errors)
    out["lines_lost"] = sum(1 for m, _ in matches if m is None)
    out["lines_duplicated"] = sum(extra for _, extra in matches)
    if len(window_specs) > 1:
        cuts = seam_cuts(window_specs)
        near = [i for i, ln in enumerate(lines) if any(abs(float(ln["start_s"]) - c) <= SEAM_ZONE_S for c in cuts)]
        out["seam_losses"] = sum(1 for i in near if matches[i][0] is None)
        out["seam_duplicates"] = sum(matches[i][1] for i in near)
        out["seam_uncertain"] = stitched.uncertain_seam
    out["interpolated_share"] = (sum(1 for s in segments if s.interpolated) / len(segments)) if segments else 0.0
    return out


def aggregate(clips: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Per family and per language the mean of each numeric metric over the clips that have it; `max` for the counts that
    must be zero. Ukrainian is its own row under `by_language`."""
    def fold(group: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
        row: Dict[str, float] = {"clips": float(len(group))}
        for key in HIGHER_IS_BETTER:
            values = [float(c[key]) for c in group if key in c]
            if not values:
                continue
            row[key] = (max(values) if key in ("seam_duplicates", "seam_losses", "coverage_resubmits", "speaker_overcount")
                        else sum(values) / len(values))
        silent = [bool(c["no_speech_ok"]) for c in group if "no_speech_ok" in c]
        if silent:
            row["no_speech_ok"] = 1.0 if all(silent) else 0.0
        return row

    by_family: Dict[str, List[Mapping[str, Any]]] = {}
    by_language: Dict[str, List[Mapping[str, Any]]] = {}
    for clip in clips:
        by_family.setdefault(str(clip["family"]), []).append(clip)
        if clip.get("language"):
            by_language.setdefault(str(clip["language"]), []).append(clip)
    return {"by_family": {k: fold(v) for k, v in sorted(by_family.items())},
            "by_language": {k: fold(v) for k, v in sorted(by_language.items())}}


def _round(value: float, up: bool) -> float:
    scale = 1_000_000
    return (math.ceil(value * scale) if up else math.floor(value * scale)) / scale


def make_bars(table: Mapping[str, Any], *, recorded_at: str, gold_sha256: str, model: str) -> Dict[str, Any]:
    """The bars of a first live run: exactly what it measured, rounded in the safe direction. Not a guess, not a target."""
    bars: Dict[str, Any] = {"recorded_at": recorded_at, "gold_sha256": gold_sha256, "model": model, "by_family": {},
                            "by_language": {}}
    for scope in ("by_family", "by_language"):
        for name, row in table[scope].items():
            bars[scope][name] = {key: (_round(value, up=not HIGHER_IS_BETTER[key]) if key in HIGHER_IS_BETTER else value)
                                 for key, value in row.items() if key in HIGHER_IS_BETTER or key in ("clips", "no_speech_ok")}
    return bars


def regressions(bars: Mapping[str, Any], table: Mapping[str, Any]) -> List[str]:
    """Every (scope, name, metric) worse than its bar, as a readable line; a scope or metric the run lacks is named too."""
    problems: List[str] = []
    for scope in ("by_family", "by_language"):
        for name, bar_row in bars.get(scope, {}).items():
            row = table.get(scope, {}).get(name)
            if row is None:
                problems.append(f"{scope}/{name}: not measured in this run")
                continue
            for key, bar in bar_row.items():
                if key == "clips":
                    continue
                if key == "no_speech_ok":
                    if bar == 1.0 and row.get(key, 0.0) < 1.0:
                        problems.append(f"{scope}/{name}: no_speech_ok fell from the bar")
                    continue
                if key not in row:
                    problems.append(f"{scope}/{name}/{key}: not measured in this run")
                    continue
                worse = row[key] < bar if HIGHER_IS_BETTER[key] else row[key] > bar
                if worse:
                    problems.append(f"{scope}/{name}/{key}: {row[key]:.6f} against the bar {bar:.6f}")
    return problems
