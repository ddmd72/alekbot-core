"""The owner's answers to a memory-review question."""
from enum import Enum


class AnswerKind(str, Enum):
    """One per option of the question, and the option's id: what the owner said about a fact he was asked about."""

    STILL_TRUE = "still_true"
    ENDED = "ended"
    NEVER = "never"
    DONT_KNOW = "dont_know"
