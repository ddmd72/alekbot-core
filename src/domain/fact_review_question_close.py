"""What closing a memory-review question did, as the transaction that closed it saw it."""
from enum import Enum


class QuestionClose(str, Enum):
    """Compare with a member; a truth test raises, so a caller written for the old bool return cannot read REFUSED as
    success."""

    REFUSED = "refused"    # nothing written: no such question, closed already, or the closer's fields do not fit
    CLOSED = "closed"      # closed; the current slot named another question, or none, and was left as it was
    RELEASED = "released"  # closed, and the current slot, which named this question, cleared in the same transaction

    def __bool__(self) -> bool:
        raise TypeError("compare a QuestionClose with a member; it has no truth value")
