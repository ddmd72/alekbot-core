"""What closing a memory-review question did, as the transaction that closed it saw it."""
from enum import Enum


class QuestionClose(str, Enum):
    """Every member is truthy: compare with a member, never test the value itself."""

    REFUSED = "refused"    # nothing written: no such question, closed already, or the closer's fields do not fit
    CLOSED = "closed"      # closed; the current slot named another question, or none, and was left as it was
    RELEASED = "released"  # closed, and the current slot, which named this question, cleared in the same transaction
