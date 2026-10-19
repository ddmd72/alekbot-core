"""The drive-search skill's rule for files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, §26.4, D33): the result
says the file exists; Smart tells the user to copy it into a new folder they create inside the bot's folder (Δ75) and
does not try to open it."""
from pathlib import Path

from src.adapters.filesystem_skill_loader import load_system_skills

SKILLS = Path(__file__).resolve().parents[3] / "src" / "skills" / "smart"


def test_the_skill_says_what_to_do_with_a_file_the_bot_cannot_open():
    body = next(s for s in load_system_skills(SKILLS) if s.name == "drive-search").body
    # the §26.4 text as amended by Δ75
    assert ("A result saying the bot cannot open the file: the file exists. Tell the user to copy each file into a "
            "new folder they create inside the bot's folder (a copy in the same folder can stay closed); the copy can "
            "be read. Do not try to open it.") in body
