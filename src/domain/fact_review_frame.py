"""The history pair around a memory-review question, as text: the frame the question is sent under, the frame that
retracts it when it was not delivered, and the marker on the owner's reply. One question id in each; the consolidation
prompt names these exact shapes, so a reader finds them with the parsers here and nowhere else."""
import re
from typing import Optional, Tuple

_ID = r"[A-Za-z0-9_\-]{1,64}"
_QUESTION_ID = re.compile(_ID)
_REVIEW_FRAME = re.compile(
    rf"\[System: memory review q=({_ID}) — Alek asked the owner about a fact he could not confirm\]"
)
_NOT_DELIVERED_FRAME = re.compile(
    rf"\[System: memory review q=({_ID}) not delivered — the owner did not see the question above\]"
)
_REPLY_PART = re.compile(rf"\[Reply to memory review q=({_ID})\] (.*)", re.DOTALL)


def _checked(question_id: str) -> str:
    if not _QUESTION_ID.fullmatch(question_id):
        raise ValueError("a question id is 1-64 characters of letters, digits, '_' and '-'")
    return question_id


def review_frame(question_id: str) -> str:
    """The user part before the question: the owner did not write it, the system says what the next message is."""
    return f"[System: memory review q={_checked(question_id)} — Alek asked the owner about a fact he could not confirm]"


def not_delivered_frame(question_id: str) -> str:
    """The user part that follows when the question was not delivered after all."""
    return f"[System: memory review q={_checked(question_id)} not delivered — the owner did not see the question above]"


def reply_part(question_id: str, text: str) -> str:
    """The owner's reply to the question, verbatim, under the marker that tells every reader whose answer it is."""
    return f"[Reply to memory review q={_checked(question_id)}] {text}"


def parse_frame(text: str) -> Optional[str]:
    """The question id of a `review_frame`, or None for anything else (an ordinary `[System:` note included)."""
    match = _REVIEW_FRAME.fullmatch(text)
    return match.group(1) if match else None


def parse_not_delivered_frame(text: str) -> Optional[str]:
    """The question id of a `not_delivered_frame`, or None."""
    match = _NOT_DELIVERED_FRAME.fullmatch(text)
    return match.group(1) if match else None


def parse_reply_part(text: str) -> Optional[Tuple[str, str]]:
    """(question id, the owner's words) of a `reply_part`, or None. The words come back exactly as given."""
    match = _REPLY_PART.fullmatch(text)
    return (match.group(1), match.group(2)) if match else None
