"""The history pair around a memory-review question, as text: the frame the question is sent under and the question's
own text after it, the frame that retracts it when it was not delivered, and the marker on the owner's reply. One question id in each. The model reads these
exact shapes, as it reads a refusal text: changing one changes what the prompts quote.

The shapes never bind an answer: an answer binds only through the question's message ref (a reply to that message, a click
on it), so typing a marker yourself applies nothing."""
import re

from .interactive_message import InteractiveMessage

_ID = r"[A-Za-z0-9_\-]{1,64}"
_QUESTION_ID = re.compile(_ID)


def _checked(question_id: str) -> str:
    if not _QUESTION_ID.fullmatch(question_id):
        raise ValueError("a question id is 1-64 characters of letters, digits, '_' and '-'")
    return question_id


def review_frame(question_id: str) -> str:
    """The user part before the question: the owner did not write it, the system says what the next message is."""
    return f"[System: memory review q={_checked(question_id)} — Alek asked the owner about a fact he could not confirm]"


def question_text(message: InteractiveMessage) -> str:
    """The model part after `review_frame`: the question message as Alek "said" it, in three lines: the question line,
    the fact in «» as the owner was shown it (translated into the message language when the review stored a translation), and the options with their digits (`options_line`, over the one digit rule `numbered_options`). The reply hint is the
    owner's, not the history's."""
    return f"{message.prompt}\n«{message.quote}»\n{message.options_line()}"


def not_delivered_frame(question_id: str) -> str:
    """The user part that follows when the question was not delivered after all."""
    return f"[System: memory review q={_checked(question_id)} not delivered — the owner did not see the question above]"


# The model part of the not-delivered pair: what Alek "said" after `not_delivered_frame`. No question text.
NOT_DELIVERED_REPLY = "(not delivered)"


def reply_part(question_id: str, text: str) -> str:
    """The owner's reply to the question, verbatim, under the marker that tells every reader whose answer it is."""
    return f"[Reply to memory review q={_checked(question_id)}] {text}"
