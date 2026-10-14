"""The drive-search skill's rule for files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, §26.4, D33): the result
says the file exists; Smart tells the user a copy inside the bot's folder can be read and does not try to open it."""
from pathlib import Path

from src.adapters.filesystem_skill_loader import load_system_skills

SKILLS = Path(__file__).resolve().parents[3] / "src" / "skills" / "smart"


def test_the_skill_says_what_to_do_with_a_file_the_bot_cannot_open():
    body = next(s for s in load_system_skills(SKILLS) if s.name == "drive-search").body
    assert ("A result saying the bot cannot open the file: the file exists. Tell the user that a copy of it inside "
            "the bot's folder can be read; do not try to open it.") in body
