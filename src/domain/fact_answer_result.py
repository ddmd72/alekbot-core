"""What applying one owner answer to a memory-review question came to."""
from enum import Enum


class AnswerResult(str, Enum):
    APPLIED = "applied"          # the fact was written and the question closed
    CHANGED = "changed"          # the fact is no longer the text the owner was asked about; nothing written
    CONFLICT = "conflict"        # another writer changed the fact meanwhile; nothing written
    NOT_FOUND = "not_found"      # no such question of this account and user
    NOT_OPEN = "not_open"        # the question is closed already
    NOT_APPLIED = "not_applied"  # the write rules refused the answer's write; nothing written
