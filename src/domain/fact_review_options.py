"""The options of a memory-review question, by its kind (P2-3): what the digits number and what each answer may be.

A chat question offers its kind's options in digit order, "don't remember" last; a digest question (a routine invoice,
answered in the Cabinet only) offers them without "don't remember"."""
from types import MappingProxyType
from typing import Final, Mapping, Tuple

from .fact_review_answer_kind import AnswerKind
from .fact_review_question import UNKNOWN_TEXT, FactReviewQuestion, QuestionKind

OPTIONS: Final[Mapping[QuestionKind, Tuple[AnswerKind, ...]]] = MappingProxyType({
    "still_true": (AnswerKind.STILL_TRUE, AnswerKind.ENDED, AnswerKind.NEVER, AnswerKind.DONT_KNOW),
    "happened": (AnswerKind.HAPPENED, AnswerKind.NOT_HAPPENED, AnswerKind.DONT_KNOW),
    "renewed": (AnswerKind.RENEWED, AnswerKind.NOT_RENEWED, AnswerKind.NOT_NEEDED, AnswerKind.DONT_KNOW),
    "paid": (AnswerKind.PAID, AnswerKind.NOT_PAID, AnswerKind.DONT_KNOW),
})


def options_of(question: FactReviewQuestion) -> Tuple[AnswerKind, ...]:
    """The answers `question` offers, in digit order: its kind's options; a digest question's without `dont_know`."""
    options = OPTIONS[question.question]
    if question.in_chat:
        return options
    return tuple(kind for kind in options if kind is not AnswerKind.DONT_KNOW)


def text_key(kind: AnswerKind) -> str:
    """The key of the text an answer writes: the answer's own value; `dont_know` writes the `unknown` text."""
    return UNKNOWN_TEXT if kind is AnswerKind.DONT_KNOW else kind.value
