"""The drive-search skill: the cannot-open line overrides the stale and accuracy rules (card answer, flagged as
possibly out of date, never an open)."""
import re
from pathlib import Path

from src.adapters.filesystem_skill_loader import load_system_skills

SKILLS = Path(__file__).resolve().parents[3] / "src" / "skills" / "smart"


def _flat() -> str:
    body = next(s for s in load_system_skills(SKILLS) if s.name == "drive-search").body
    return re.sub(r"\s+", " ", body)


def test_the_access_line_overrides_the_stale_and_accuracy_rules():
    body = _flat()
    sentence = ("This line overrides the stale and accuracy rules: answer from the card and say it may be out "
                "of date; do not open the file.")
    assert body.count(sentence) == 1
    bullet = body[body.index("A result saying the bot cannot open the file"):]
    assert sentence in bullet.split("## When nothing is found")[0]
