"""The drive-search skill's cannot-open line, after the cut (DRIVE_SPECIALIST_RFC §5.7, §7.3): it reaches Smart as the
bot-cannot-open reading line, and still overrides the stale and accuracy rules (card answer, flagged as possibly out of
date, never an open)."""
import re

from src.domain import drive_answer_texts as t


def _flat() -> str:
    return re.sub(r"\s+", " ", t.R_NO_ACCESS)


def test_the_access_line_overrides_the_stale_and_accuracy_rules():
    body = _flat()
    sentence = ("This line overrides the stale and accuracy rules: answer from the card and say it may be out "
                "of date; do not open the file.")
    assert body.count(sentence) == 1
    bullet = body[body.index("A result saying the bot cannot open the file"):]
    assert sentence in bullet
