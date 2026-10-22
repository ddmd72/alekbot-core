"""The drive-search skill's "Card accuracy" section (USER_DRIVE_INDEXING_RFC §26.4, D36): verbatim from the RFC (line
wraps aside), one copy of each sentence, the 90% bar only in the skill text."""
import re
from pathlib import Path

from src.adapters.filesystem_skill_loader import load_system_skills

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "src" / "skills" / "smart"


def _body() -> str:
    return next(s for s in load_system_skills(SKILLS) if s.name == "drive-search").body


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_the_section_is_there_with_its_rule_lines():
    body = _flat(_body())
    for line in (
        "## Card accuracy",
        "A described file's result carries `card accuracy: N%`. 100% — everything in this card is confirmed by the "
        "file's own text; you can trust the card fully. The lower, the more likely the card's values contain errors.",
        "\"Find\", \"which files\", \"do I have\": answer from the cards at any accuracy. Never open files for this.",
        "A question about a value in a file — a number, a date, an amount, an expiry: answer from the card when its "
        "accuracy is 90% or more. Below that, open the file and check the value before answering.",
        "For a scan or a photo of a document, the file's text is a reading of the image; when a value matters to the "
        "user, say where you read it.",
    ):
        assert line in body, line


def test_the_inaccessible_file_bullet_is_the_sections_last_and_stands_once():
    body = _body()
    bullet = "A result saying the bot cannot open the file: the file exists."
    assert body.count(bullet) == 1
    section = body[body.index("## Card accuracy"):body.index("## When nothing is found")]
    assert section.rstrip().splitlines()[-1].startswith(f"- {bullet}")


def test_each_rule_sentence_has_one_copy():
    """Retargeted at the cut: the section's sentences reach Smart as reading lines (DRIVE_SPECIALIST_RFC §5.7, §7.3)."""
    from src.domain import drive_answer_texts as t
    body = _flat("\n".join(v for k, v in vars(t).items() if k.startswith("R_") and isinstance(v, str)))
    for sentence in ("answer from the cards at any accuracy", "open the file and check the value before answering",
                     "say where you read it"):
        assert body.count(sentence) == 1, sentence
