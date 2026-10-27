"""What the owner said about a fact he was asked about."""
from enum import Enum
from typing import Final


class AnswerKind(str, Enum):
    """One per option of a question (each kind offers its own, `fact_review_options`), and the option's id. A module of its own: the question record, the trail reasons
    and the rules all name it, and `fact_review_answers` builds on all three."""

    STILL_TRUE = "still_true"
    ENDED = "ended"
    NEVER = "never"
    DONT_KNOW = "dont_know"
    # P2-3: the options of a `happened`, `renewed` or `paid` question (`fact_review_options.OPTIONS`).
    HAPPENED = "happened"
    NOT_HAPPENED = "not_happened"
    RENEWED = "renewed"
    NOT_RENEWED = "not_renewed"
    NOT_NEEDED = "not_needed"
    PAID = "paid"
    NOT_PAID = "not_paid"


# The owner's answers that write one of the question's texts as history (P2-3): read by the trail reasons and by the write.
HISTORY_ANSWERS: Final[frozenset[AnswerKind]] = frozenset({
    AnswerKind.HAPPENED, AnswerKind.NOT_HAPPENED, AnswerKind.NOT_NEEDED, AnswerKind.PAID, AnswerKind.NOT_PAID,
})
