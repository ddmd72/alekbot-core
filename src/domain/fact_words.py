"""How long a fact's text may be, and the one way its words are counted (the review's `count_words` tool counts the same).

A text the review writes for later (a history rewrite, the texts of a question) is checked here before it is accepted:
one line, no `[` (it would imitate a history frame), at most MAX_FACT_WORDS words."""
from typing import Final, Optional

MAX_FACT_WORDS: Final[int] = 40


def word_count(text: str) -> int:
    """The words of `text`: its whitespace-separated runs."""
    return len(text.split())


def written_text_problem(text: object) -> Optional[str]:
    """Why `text` may not be a text the review writes, or None: not a non-empty text, more than one line, a `[`, or more
    than MAX_FACT_WORDS words."""
    if not isinstance(text, str) or not text.strip():
        return "not a text"
    if len(text.strip().splitlines()) != 1:
        return "more than one line"
    if "[" in text:
        return "has ["
    words = word_count(text)
    if words > MAX_FACT_WORDS:
        return f"{words} words, more than {MAX_FACT_WORDS}"
    return None
