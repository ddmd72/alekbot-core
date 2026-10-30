"""
Drive note view — what `get_note` shows of one note: the current text and classifiers, never a version body
(specialist RFC §9.2, design §4.1). Pure: domain modules only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from src.domain.drive_note import DriveNote, NoteState, item_ref


@dataclass(frozen=True)
class NoteView:
    note_id: str
    ref: str                  # `drive:<tag>:<item id>` of the bound item ("" for a stub)
    note_ref: str             # `drive_note:<note id>`
    path: str                 # from the snapshot ("" for a stub)
    version: int
    state: str
    text: str = ""
    doc_type: str = ""
    life_areas: Tuple[str, ...] = ()

    @property
    def cleared(self) -> bool:
        return self.state == NoteState.CLEARED.value

    @property
    def archived(self) -> bool:
        return self.state == NoteState.ARCHIVED.value

    @classmethod
    def of(cls, note: DriveNote) -> "NoteView":
        state = note.state.value if isinstance(note.state, NoteState) else str(note.state)
        return cls(note_id=note.note_id, ref=item_ref(note), note_ref=note.ref,
                   path=note.snapshot.path if note.snapshot is not None else "", version=note.current_version,
                   state=state, text=note.text, doc_type=note.doc_type, life_areas=tuple(note.life_areas))


__all__ = ["NoteView"]
