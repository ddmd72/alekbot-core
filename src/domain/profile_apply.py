"""What applying an owner's reply to a profile question came to (fact profile RFC §4.7)."""
from enum import Enum


class ProfileApply(str, Enum):
    APPLIED = "applied"      # the answer was recorded, or its key already had one (the first answer wins)
    NOT_READ = "not_read"    # the reply is no option and no form code reads: nothing recorded, the question stays open
