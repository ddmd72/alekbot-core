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
