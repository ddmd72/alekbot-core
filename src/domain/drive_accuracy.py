"""
Drive accuracy — the one score a card carries (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §26.4, D35, Δ61). Pure: no I/O,
no clock.

    accuracy = clamp(round(100 * (1 - S) * (1 - V)) - R, 0, 100)

`V = sum(w * m) / sum(w)` over the graded values (0 when there are none): weight `w` by field, miss `m` by grade.
`S` is the text's source, a fixed small share: a native text layer 0, a screenshot 0.03, a scan (read by a model, or
its OCR layer) 0.05, a document photo 0.08, a model-read file of unknown capture 0.05, a model-read file called digital 0.03
(Δ85: the text is a reading, not a text layer). `R` is fixed points for reading
problems: pages refused 5, read by the fallback model 3, truncated 2.

Every weight is in the tables below, hashed by `weights_digest` and pinned in a test to `ACCURACY_VERSION`: a change to
a weight is a version bump, which re-scores from the stored profile and text (no download, no model call, no re-embed:
the score is never in the card text).

A photograph and a card with nothing model-written to check (a folder, a file with no readable text, a template
profile with no brief) carry no score (`None`).

Public interface:
    ACCURACY_VERSION
    FIELD_WEIGHTS, GRADE_MISS, SOURCE_SHARE, READING_POINTS, weights_digest() -> str
    verification_miss(graded) -> float                 V
    source_share(rec) -> float                         S
    reading_points(rec) -> int                         R
    accuracy(graded, *, source, points) -> int         the formula
    card_graded(rec, chunks) -> Optional[Tuple[GradedValue, ...]]   the grades the card's score reads, or None: no score
    card_accuracy(rec, graded) -> Optional[int]        the score of a record, given `card_graded`'s grades
    scored(rec, chunks) -> DriveIndexRecord            the record with its score and `accuracy_version` written
    accuracy_settled(rec) -> bool                      the upgrade pass's test: scored under this version, or no score
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Mapping, Optional, Sequence, Tuple

from .drive_capture import Capture
from .drive_enrichment import Grade, GradedField, GradedValue, ProfileSource
from .drive_extraction import READER
from .drive_index import ACCURACY_VERSION as ACCURACY_VERSION, DriveIndexRecord, TriageVerdict
from .drive_profile import _HasText, grade_brief

FIELD_WEIGHTS: Mapping[str, int] = {
    GradedField.DATE.value: 3, GradedField.VALUE.value: 3, GradedField.NUMBER.value: 3,
    GradedField.ENTITY.value: 2, GradedField.TOPIC_NATIVE.value: 1,
}
GRADE_MISS: Mapping[str, float] = {Grade.IN_TEXT.value: 0.0, Grade.IN_TEXT_AS.value: 0.1, Grade.NOT_IN_TEXT.value: 1.0}
# The text's source, by capture; "model_unknown" is a model-read file whose capture no one named.
SOURCE_SHARE: Mapping[str, float] = {
    "native": 0.0, Capture.DIGITAL.value: 0.0, Capture.SCREENSHOT.value: 0.03, Capture.SCAN.value: 0.05,
    Capture.DOCUMENT_PHOTO.value: 0.08, "model_unknown": 0.05,
    "digital_model_read": 0.03,   # Δ85: a model read a file it called digital: priced like a screenshot, not native text
}
READING_POINTS: Mapping[str, int] = {"pages_refused": 5, "read_by_fallback": 3, "truncated": 2}


def weights_digest() -> str:
    """The tables above, hashed; a test pins it to `ACCURACY_VERSION`."""
    tables = (sorted(FIELD_WEIGHTS.items()), sorted(GRADE_MISS.items()), sorted(SOURCE_SHARE.items()),
              sorted(READING_POINTS.items()))
    return hashlib.sha256(repr(tables).encode("utf-8")).hexdigest()


def verification_miss(graded: Sequence[GradedValue]) -> float:
    """V: the weighted share of the graded values the text does not confirm; 0 when nothing is graded. A value of a
    field or grade this version does not know weighs nothing (a newer row read by an older build)."""
    total = sum(FIELD_WEIGHTS.get(g.field, 0) for g in graded)
    if total <= 0:
        return 0.0
    return sum(FIELD_WEIGHTS.get(g.field, 0) * GRADE_MISS.get(g.grade, 0.0) for g in graded) / total


def model_read(rec: DriveIndexRecord) -> bool:
    """A model produced the file's text: the reader's link decided it (`extracted_by` = `reader@<version>`)."""
    return (rec.extracted_by or "").startswith(f"{READER}@")


def source_share(rec: DriveIndexRecord) -> float:
    """S: the capture decides; without one, a model-read file is priced as unknown, a parser-read one as native text."""
    if rec.capture == Capture.DIGITAL.value and model_read(rec):
        return SOURCE_SHARE["digital_model_read"]
    if rec.capture:
        return SOURCE_SHARE.get(rec.capture, SOURCE_SHARE["model_unknown"] if model_read(rec) else 0.0)
    return SOURCE_SHARE["model_unknown"] if model_read(rec) else SOURCE_SHARE["native"]


def reading_points(rec: DriveIndexRecord) -> int:
    """R: refused pages, the fallback model and a truncated read each cost their points once."""
    return (READING_POINTS["pages_refused"] * (rec.pages_refused > 0)
            + READING_POINTS["read_by_fallback"] * bool(rec.read_by_fallback)
            + READING_POINTS["truncated"] * bool(rec.truncated))


def accuracy(graded: Sequence[GradedValue], *, source: float, points: int) -> int:
    """The formula, clamped to 0-100."""
    score = round(100 * (1 - source) * (1 - verification_miss(graded))) - points
    return max(0, min(100, score))


def card_graded(rec: DriveIndexRecord, chunks: Sequence[_HasText]) -> Optional[Tuple[GradedValue, ...]]:
    """The grades a record's score reads (§26.4 "What carries it"), or None when it carries no score: a folder, a
    photograph, a file with no readable text, or a card with nothing model-written to check. A model-profiled file
    carries its profile's grades; a template profile has none of its own, so a brief-only file (or one whose profile
    is a template) carries its reader brief's date, amount and parties graded against `chunks`."""
    if rec.is_folder or rec.triage_verdict is TriageVerdict.PHOTO or rec.indexed_generation is None:
        return None
    if rec.profile is not None and rec.profile_source != ProfileSource.TEMPLATE.value:
        return rec.profile.graded
    if rec.brief is not None:
        return grade_brief(rec.brief, chunks, rec.name)
    return None


def card_accuracy(rec: DriveIndexRecord, graded: Optional[Sequence[GradedValue]]) -> Optional[int]:
    """The record's score from `card_graded`'s grades (None in, None out), its capture and extractor, and its reading
    problems."""
    if graded is None:
        return None
    return accuracy(graded, source=source_share(rec), points=reading_points(rec))


def scored(rec: DriveIndexRecord, chunks: Sequence[_HasText]) -> DriveIndexRecord:
    """The record with its score and the version it was computed under (§26.4): written wherever a write changes the
    profile, the brief, the capture or the reading facts. Pure on the stored grades (a brief-only card grades its
    brief against `chunks`); the score is never in the card text, so this never re-embeds."""
    return replace(rec, card_accuracy=card_accuracy(rec, card_graded(rec, chunks)), accuracy_version=ACCURACY_VERSION)


def scorable(rec: DriveIndexRecord) -> bool:
    """The record carries model-written content checked against text (§26.4 "What carries it"): a readable file with
    a model profile, or a brief. Decided without chunks; `card_graded` decides the same from the grades."""
    if rec.is_folder or rec.triage_verdict is TriageVerdict.PHOTO or rec.indexed_generation is None:
        return False
    if rec.profile is not None and rec.profile_source != ProfileSource.TEMPLATE.value:
        return True
    return rec.brief is not None


def accuracy_settled(rec: DriveIndexRecord) -> bool:
    """The upgrade pass's test for the `accuracy` key (§26.4 "Recomputing"): scored under this version, or a record
    that carries no score (a folder, a photograph, a metadata-only card, a file whose profile is still to come: the
    write that gives it a profile or brief scores it)."""
    return rec.accuracy_version == ACCURACY_VERSION or not scorable(rec)
