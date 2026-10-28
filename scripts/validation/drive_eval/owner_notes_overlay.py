"""
Owner notes for the answer-from-content eval (USER_DRIVE_INDEXING_RFC §28, plan CTX.5). The eval tree format has no
owner-note fields and the notes must not enter the index cache (a note change would cost a paid rebuild), so they are
applied AFTER `index_cache.load_or_build` returns, onto the in-memory records, through `with_owner_note` — the same
merge the indexer does (`src/domain/drive_note_merge.py`). This module is not in `index_cache._CODE`.

    apply_owner_notes(repo, load_owner_notes(path))   # -> the number of records that now carry a note

Only `specialist_ab` calls it. Nothing is written to Firestore or to the drive.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Sequence, Tuple

from src.domain.drive_labels import DOC_TYPES, LIFE_DOMAINS
from src.domain.drive_note_merge import NoteView, with_owner_note

from .fixture_drive import item_id_for

NOTE_ID_PREFIX = "eval-note-"


class OwnerNotesError(ValueError):
    """The notes file is malformed or names a path the drive does not hold."""


@dataclass(frozen=True)
class EvalOwnerNote:
    path: str
    text: str
    doc_type: str = ""
    life_areas: Tuple[str, ...] = ()


def parse_owner_notes(payload: Mapping[str, Any]) -> List[EvalOwnerNote]:
    """The notes of a parsed file; raises OwnerNotesError listing every problem."""
    problems: List[str] = []
    notes: List[EvalOwnerNote] = []
    seen = set()
    for index, raw in enumerate(payload.get("notes") or []):
        path, text = str(raw.get("path", "")).strip("/"), str(raw.get("text", "")).strip()
        doc_type, areas = str(raw.get("doc_type", "")), tuple(raw.get("life_areas") or ())
        if not path or not text:
            problems.append(f"note {index}: path and text are required")
        if path in seen:
            problems.append(f"note {index}: {path!r} is noted twice")
        if doc_type and doc_type not in DOC_TYPES:
            problems.append(f"note {index}: unknown doc_type {doc_type!r}")
        bad = [a for a in areas if a not in LIFE_DOMAINS]
        if bad:
            problems.append(f"note {index}: unknown life areas {bad}")
        seen.add(path)
        notes.append(EvalOwnerNote(path=path, text=text, doc_type=doc_type, life_areas=areas))
    if problems:
        raise OwnerNotesError("malformed owner notes: " + "; ".join(problems))
    return notes


def load_owner_notes(path: Path) -> List[EvalOwnerNote]:
    return parse_owner_notes(json.loads(Path(path).read_text(encoding="utf-8")))


def apply_owner_notes(repo: Any, notes: Sequence[EvalOwnerNote]) -> int:
    """Put each note on its record in `repo.records` (an InMemoryDriveIndexRepository) for every user that holds the
    item. Raises OwnerNotesError when a path has no record (a typo would otherwise silently drop the case's premise)."""
    applied = 0
    for number, note in enumerate(notes, start=1):
        item_id = item_id_for(note.path)
        view = NoteView(note_id=f"{NOTE_ID_PREFIX}{number}", version=1, text=note.text, doc_type=note.doc_type,
                        life_areas=tuple(note.life_areas))
        keys = [key for key in repo.records if key[1] == item_id]
        if not keys:
            raise OwnerNotesError(f"no index record for {note.path!r}")
        for key in keys:
            repo.records[key] = with_owner_note(repo.records[key], view)
            applied += 1
    return applied
