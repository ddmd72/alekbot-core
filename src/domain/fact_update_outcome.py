"""What one update of a fact came to: written, or why nothing was (Facts S4 T3c review M2).

Code reads this; the model reads the dict `domain.fact_tool_result.update_result` renders from it."""
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional, Tuple


class UpdateRefusal(str, Enum):
    """Why an update wrote nothing."""

    NOT_FOUND = "not_found"      # no readable fact of the account under the id, in a state the pass may write
    INVALID = "invalid"          # the update breaks a rule its arguments are held to (an invalid argument)
    HISTORY = "history"          # the history fact may not go with this write, or the fact moved since it was asked
    EXPIRY = "expiry"            # an expiry the rules refuse
    BORN_DUE = "born_due"        # a well-formed expiry that is already due
    UNRESOLVED = "unresolved"    # the id's chain ends in a merge, has no successor, or ends elsewhere
    MOVED = "moved"              # the fact moved forward between two attempts
    CONFLICT = "conflict"        # another writer changed the fact twice in a row
    ERROR = "error"              # anything else that failed


# The refusals the expiry rules give: decided on the update itself, whatever the stored fact is.
EXPIRY_REFUSALS: FrozenSet[UpdateRefusal] = frozenset({UpdateRefusal.EXPIRY, UpdateRefusal.BORN_DUE})


@dataclass(frozen=True)
class FactUpdateOutcome:
    """`fact_id` is the id the update was asked for. Written: `written_id` and `version` are the document written (a new
    version has a new id), `current_id` the current document the update was applied to (the id followed forward, when it
    had been replaced), `history_fact_id` the fact created beside an archive, and `notes` the domain's notes, each one
    whole note. Not written: `refusal` and its text."""

    fact_id: str
    written_id: Optional[str] = None
    version: Optional[int] = None
    current_id: Optional[str] = None
    history_fact_id: Optional[str] = None
    notes: Tuple[str, ...] = ()
    refusal: Optional[UpdateRefusal] = None
    refusal_text: Optional[str] = None

    def __post_init__(self) -> None:
        if self.refusal is None:
            if self.written_id is None or self.version is None or self.current_id is None:
                raise ValueError("a written update names the document written, its version and the one it applied to")
            if self.refusal_text is not None:
                raise ValueError("a written update has no refusal text")
        else:
            if self.refusal_text is None:
                raise ValueError("a refused update carries its refusal text")
            if any(v is not None for v in (self.written_id, self.version, self.current_id, self.history_fact_id)):
                raise ValueError("a refused update wrote nothing")
            if self.notes:
                raise ValueError("a refused update has no notes")

    @classmethod
    def refused(cls, fact_id: str, refusal: UpdateRefusal, text: str) -> "FactUpdateOutcome":
        return cls(fact_id=fact_id, refusal=refusal, refusal_text=text)

    @property
    def written(self) -> bool:
        return self.refusal is None

    @property
    def resolved_from(self) -> Optional[str]:
        """The id asked for, when it had been replaced and the update was applied to its current version; else None."""
        return self.fact_id if self.current_id is not None and self.current_id != self.fact_id else None
