"""What recording an owner's reply in words did, as the transaction that recorded it saw it."""
from enum import Enum


class ReplyRecord(str, Enum):
    """Compare with a member; a truth test raises, so a caller written for the old bool return cannot read REFUSED as
    success."""

    REFUSED = "refused"      # nothing written: no such question, not asked/unanswered, reply already shown, no words, nothing more fits
    RECORDED = "recorded"    # recorded; the current slot named another question, or none, and was left as it was
    RELEASED = "released"    # recorded, and the current slot, which named this question, cleared in the same transaction

    def __bool__(self) -> bool:
        raise TypeError("compare a ReplyRecord with a member; it has no truth value")
