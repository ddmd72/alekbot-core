"""What the owner said about a fact he was asked about."""
from enum import Enum


class AnswerKind(str, Enum):
    """One per option of the question, and the option's id. A module of its own: the question record, the trail reasons
    and the rules all name it, and `fact_review_answers` builds on all three."""

    STILL_TRUE = "still_true"
    ENDED = "ended"
    NEVER = "never"
    DONT_KNOW = "dont_know"
