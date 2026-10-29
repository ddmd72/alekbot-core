"""What a drive read showed (DRIVE_SPECIALIST_RFC §7.3): the facts the reading lines are chosen from."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadFacts:
    """What a read showed, decided by code from the typed result (never from its text): the triggers of §7.3."""
    cards: int = 0                       # search cards shown
    passage: bool = False                # a card carries a passage
    accuracy: bool = False               # a card carries `card accuracy: N%`
    scan: bool = False                   # a card's capture is a scan or a photo of a document
    inaccessible: bool = False           # a card, or the index's access note, says the bot cannot open the file
    image: bool = False                  # an image card
    coverage_incomplete: bool = False    # the coverage line names something not searched
    building: bool = False               # the index is still being built or rebuilt
    owner_note: bool = False             # a card, or an opened file's own block, carries the owner's note
    excerpts: bool = False               # a passages result returned passage text (§28.3 A13)
    low_accuracy_image: bool = False     # a card below 90% whose text is not known to be born-digital (§28 OQ-A2)
    paged: bool = False                  # a page of a file's indexed text was read, `from N` (§29.5)
    too_long: bool = False               # an open was refused: the text is above the open bound (§29.2)
    cut: bool = False                    # an open was cut at the open bound: the body is the file's head (§29.2)
