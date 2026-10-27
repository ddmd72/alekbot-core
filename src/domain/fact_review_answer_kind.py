"""What the owner said about a fact he was asked about."""
from enum import Enum


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
