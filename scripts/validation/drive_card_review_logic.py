"""
Card-accuracy review, the pure half (USER_DRIVE_INDEXING_RFC §26.6 D37, §26.4): no I/O, no clock, no logging.

`scripts/validation/drive_card_review.py` reads the user's records (and the chunks of brief-only cards) and everything
it decides is decided here, so it is unit-testable on a synthetic snapshot.

Public interface:
    shape_of(printed) -> str                                  digits `9`, letters `A`, separators kept
    ReviewValue, ReviewCard, build_card(rec, graded) -> ReviewCard
    WeightTables, default_tables(), load_tables(raw) -> WeightTables      a candidate weight table (validated)
    rescore(card, tables) -> int                              the card's score under a table (= drive_accuracy.accuracy)
    lowest_cards(cards, below) -> [ReviewCard]                item 1
    shape_groups(cards) -> [ShapeGroup]                       item 2
    distributions(cards, scores) -> {dimension: {class: Distribution}}      item 3
    compare(cards, old, new) -> Comparison                    item 4
    render_report(...) -> str
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.drive_accuracy import FIELD_WEIGHTS, GRADE_MISS, READING_POINTS, SOURCE_SHARE, model_read
from src.domain.drive_enrichment import Grade, GradedValue
from src.domain.drive_index import DriveIndexRecord

DEFAULT_BELOW = 90
LOWEST_CAP = 50                  # cards listed in item 1
SHAPE_EXAMPLES = 3               # printed examples kept per shape
NO_CAPTURE = "(none)"
NO_DOC_TYPE = "(none)"
NO_EXTRACTOR = "(none)"


def shape_of(printed: str) -> str:
    """The shape of a printed form: every digit `9`, every letter (any script) `A`, anything else kept as it is."""
    return "".join("9" if ch.isdigit() else "A" if ch.isalpha() else ch for ch in printed)


@dataclass(frozen=True)
class ReviewValue:
    field: str
    value: str
    grade: str
    seen_as: str
    printed: str            # the form the document prints, where the profile stored one; else the graded value


@dataclass(frozen=True)
class ReviewCard:
    item_id: str
    name: str
    doc_type: str
    capture: str
    extractor: str          # `extracted_by` up to `@`
    fallback: bool
    stored_score: Optional[int]
    source_key: str         # the SOURCE_SHARE key the record's source resolves to
    points_keys: Tuple[str, ...]   # the READING_POINTS keys that apply
    values: Tuple[ReviewValue, ...]


def _printed_forms(rec: DriveIndexRecord) -> Dict[str, str]:
    forms: Dict[str, str] = {}
    if rec.profile is not None:
        for d in rec.profile.dates:
            if d.printed:
                forms[d.iso.isoformat()] = d.printed
    return forms


def _source_key(rec: DriveIndexRecord) -> str:
    """The key `drive_accuracy.source_share` looks up, for this record."""
    fallback = "model_unknown" if model_read(rec) else "native"
    if rec.capture and rec.capture in SOURCE_SHARE:
        return rec.capture
    return fallback


def build_card(rec: DriveIndexRecord, graded: Sequence[GradedValue]) -> ReviewCard:
    forms = _printed_forms(rec)
    keys = tuple(k for k, hit in (("pages_refused", rec.pages_refused > 0), ("read_by_fallback", rec.read_by_fallback),
                                  ("truncated", rec.truncated)) if hit)
    return ReviewCard(
        item_id=rec.item_id, name=rec.name,
        doc_type=rec.profile.doc_type if rec.profile is not None and rec.profile.doc_type else NO_DOC_TYPE,
        capture=rec.capture or NO_CAPTURE,
        extractor=(rec.extracted_by or "").split("@", 1)[0] or NO_EXTRACTOR,
        fallback=bool(rec.read_by_fallback), stored_score=rec.card_accuracy,
        source_key=_source_key(rec), points_keys=keys,
        values=tuple(ReviewValue(g.field, g.value, g.grade, g.seen_as, forms.get(g.value, g.value)) for g in graded),
    )


# -- weight tables ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WeightTables:
    field_weights: Mapping[str, float]
    grade_miss: Mapping[str, float]
    source_share: Mapping[str, float]
    reading_points: Mapping[str, float]


def default_tables() -> WeightTables:
    return WeightTables(dict(FIELD_WEIGHTS), dict(GRADE_MISS), dict(SOURCE_SHARE), dict(READING_POINTS))


_TABLE_NAMES = ("field_weights", "grade_miss", "source_share", "reading_points")


class WeightsError(ValueError):
    """The candidate weight file is not usable; the message says what to fix."""


def load_tables(raw: Any) -> WeightTables:
    """A candidate table from parsed JSON: `{"field_weights": {...}, "grade_miss": {...}, "source_share": {...},
    "reading_points": {...}}`. A table left out, or a key left out of a table, keeps the current value. An unknown
    table name or key, or a value that is not a non-negative number, is refused."""
    if not isinstance(raw, dict):
        raise WeightsError("the weights file must hold one JSON object of tables")
    unknown = sorted(set(raw) - set(_TABLE_NAMES))
    if unknown:
        raise WeightsError(f"unknown table(s) {unknown}; known: {list(_TABLE_NAMES)}")
    base = default_tables()
    merged: Dict[str, Dict[str, float]] = {}
    for name in _TABLE_NAMES:
        current: Mapping[str, float] = getattr(base, name)
        table = dict(current)
        given = raw.get(name, {})
        if not isinstance(given, dict):
            raise WeightsError(f"table {name!r} must be an object")
        bad = sorted(set(given) - set(current))
        if bad:
            raise WeightsError(f"unknown key(s) {bad} in table {name!r}; known: {sorted(current)}")
        for key, value in given.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise WeightsError(f"{name}.{key} must be a non-negative number, got {value!r}")
            table[key] = value
        merged[name] = table
    return WeightTables(**merged)


def rescore(card: ReviewCard, tables: WeightTables) -> int:
    """`drive_accuracy.accuracy` under `tables`: clamp(round(100 (1 - S)(1 - V)) - R, 0, 100)."""
    total = sum(tables.field_weights.get(v.field, 0) for v in card.values)
    miss = 0.0
    if total > 0:
        miss = sum(tables.field_weights.get(v.field, 0) * tables.grade_miss.get(v.grade, 0.0)
                   for v in card.values) / total
    source = tables.source_share.get(card.source_key, 0.0)
    points = sum(tables.reading_points.get(k, 0) for k in card.points_keys)
    return max(0, min(100, round(100 * (1 - source) * (1 - miss)) - points))


# -- the four lists --------------------------------------------------------------------------------------------------

def lowest_cards(cards: Sequence[ReviewCard], scores: Mapping[str, int], below: int) -> List[ReviewCard]:
    """Item 1: cards scoring below `below`, lowest first (name breaks ties)."""
    low = [c for c in cards if scores[c.item_id] < below]
    return sorted(low, key=lambda c: (scores[c.item_id], c.name, c.item_id))


@dataclass(frozen=True)
class ShapeGroup:
    field: str
    shape: str
    files: int
    values: int
    examples: Tuple[str, ...]


def shape_groups(cards: Sequence[ReviewCard]) -> List[ShapeGroup]:
    """Item 2: `not_in_text` values by field and shape of the printed form; files counted once per group."""
    files: Dict[Tuple[str, str], set] = defaultdict(set)
    values: Dict[Tuple[str, str], int] = defaultdict(int)
    examples: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for card in cards:
        for v in card.values:
            if v.grade != Grade.NOT_IN_TEXT.value:
                continue
            key = (v.field, shape_of(v.printed))
            files[key].add(card.item_id)
            values[key] += 1
            if v.printed not in examples[key] and len(examples[key]) < SHAPE_EXAMPLES:
                examples[key].append(v.printed)
    groups = [ShapeGroup(f, s, len(files[(f, s)]), values[(f, s)], tuple(examples[(f, s)])) for f, s in files]
    return sorted(groups, key=lambda g: (-g.files, -g.values, g.field, g.shape))


@dataclass(frozen=True)
class Distribution:
    count: int
    mean: float
    minimum: int
    median: float
    maximum: int
    below_90: int


def _distribution(scores: Sequence[int]) -> Distribution:
    s = sorted(scores)
    n = len(s)
    median = float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return Distribution(n, sum(s) / n, s[0], median, s[-1], sum(1 for x in s if x < DEFAULT_BELOW))


DIMENSIONS = ("doc_type", "capture", "extractor", "fallback")


def _dimension_value(card: ReviewCard, dimension: str) -> str:
    if dimension == "fallback":
        return "read by fallback" if card.fallback else "primary model / parser"
    return str(getattr(card, dimension))


def distributions(cards: Sequence[ReviewCard], scores: Mapping[str, int]) -> Dict[str, Dict[str, Distribution]]:
    """Item 3: the score distribution per class of each dimension."""
    out: Dict[str, Dict[str, Distribution]] = {}
    for dim in DIMENSIONS:
        buckets: Dict[str, List[int]] = defaultdict(list)
        for card in cards:
            buckets[_dimension_value(card, dim)].append(scores[card.item_id])
        out[dim] = {k: _distribution(v) for k, v in sorted(buckets.items())}
    return out


@dataclass(frozen=True)
class CardDelta:
    card: ReviewCard
    old: int
    new: int


@dataclass(frozen=True)
class Comparison:
    deltas: Tuple[CardDelta, ...]                 # cards whose score moved, largest move first
    old_dist: Dict[str, Dict[str, Distribution]]
    new_dist: Dict[str, Dict[str, Distribution]]


def compare(cards: Sequence[ReviewCard], old: Mapping[str, int], new: Mapping[str, int]) -> Comparison:
    """Item 4: per-card old vs new score and the distributions under each."""
    moved = [CardDelta(c, old[c.item_id], new[c.item_id]) for c in cards if old[c.item_id] != new[c.item_id]]
    moved.sort(key=lambda d: (-abs(d.new - d.old), d.card.name, d.card.item_id))
    return Comparison(tuple(moved), distributions(cards, old), distributions(cards, new))


# -- the report ------------------------------------------------------------------------------------------------------

def _dist_table(dists: Mapping[str, Mapping[str, Distribution]], other: Optional[Mapping[str, Mapping[str, Distribution]]]
                = None) -> List[str]:
    lines: List[str] = []
    for dim, classes in dists.items():
        lines += [f"### by {dim}", "", "| class | cards | mean | min | median | max | below 90 |"
                  + (" new mean | mean delta |" if other else ""), "|---|---|---|---|---|---|---|"
                  + ("---|---|" if other else "")]
        for name, d in classes.items():
            row = f"| {name} | {d.count} | {d.mean:.1f} | {d.minimum} | {d.median:g} | {d.maximum} | {d.below_90} |"
            if other:
                n = other[dim][name]
                row += f" {n.mean:.1f} | {n.mean - d.mean:+.1f} |"
            lines.append(row)
        lines.append("")
    return lines


def render_report(*, user_id: str, below: int, cards: Sequence[ReviewCard], scores: Mapping[str, int],
                  stale_stored: int, comparison: Optional[Comparison] = None, weights_name: str = "") -> str:
    out: List[str] = [f"# Drive card review — user {user_id}", "",
                      f"{len(cards)} scored cards; {stale_stored} carry a stored score that differs from the current "
                      f"tables (a stale `accuracy_version`). Scores below are the current tables' scores.", ""]
    low = lowest_cards(cards, scores, below)
    out += [f"## 1. Lowest-scoring cards (below {below}): {len(low)}", ""]
    for card in low[:LOWEST_CAP]:
        extra = f", source {card.source_key}" + (f", reading: {', '.join(card.points_keys)}" if card.points_keys else "")
        out.append(f"- **{scores[card.item_id]}** {card.name} (`{card.item_id}`; {card.doc_type}, {card.capture}{extra})")
        for v in card.values:
            seen = f" (seen as {v.seen_as!r})" if v.seen_as else ""
            out.append(f"    - {v.field}: {v.value!r} -> {v.grade}{seen}")
    if len(low) > LOWEST_CAP:
        out.append(f"- … {len(low) - LOWEST_CAP} more")
    out += ["", "## 2. `not_in_text` values by field and shape of the printed form", "",
            "A shape recurring across files is a format the grader or the normaliser misses: a rule to fix, not a card.",
            "", "| field | shape | files | values | examples |", "|---|---|---|---|---|"]
    for g in shape_groups(cards):
        out.append(f"| {g.field} | `{g.shape}` | {g.files} | {g.values} | {'; '.join(repr(e) for e in g.examples)} |")
    out += ["", "## 3. Score distributions", ""] + _dist_table(distributions(cards, scores))
    if comparison is not None:
        out += [f"## 4. Candidate weights ({weights_name}): {len(comparison.deltas)} of {len(cards)} cards move", "",
                "| card | old | new | delta |", "|---|---|---|---|"]
        for d in comparison.deltas[:LOWEST_CAP]:
            out.append(f"| {d.card.name} (`{d.card.item_id}`) | {d.old} | {d.new} | {d.new - d.old:+d} |")
        if len(comparison.deltas) > LOWEST_CAP:
            out.append(f"| … {len(comparison.deltas) - LOWEST_CAP} more | | | |")
        out += ["", "Distribution deltas (old mean, new mean):", ""] + _dist_table(comparison.old_dist, comparison.new_dist)
    return "\n".join(out) + "\n"
