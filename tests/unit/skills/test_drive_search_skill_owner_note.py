"""The drive-search skill's reading of the owner's note (DRIVE_FILE_NOTES_RFC §6.2): the `noted by the owner:` line
under "Reading the results", once, and the card-accuracy exception for the note."""
import re
from pathlib import Path

from src.adapters.filesystem_skill_loader import load_system_skills

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "src" / "skills" / "smart"
NOTE_LINE = ("A `noted by the owner:` line is what the user told you about the file. It outranks the card's own "
             "description and type, and it is not part of `card accuracy`.")
ACCURACY = "100% — everything in this card *except the owner's note* is confirmed by the file's own text"


def _body() -> str:
    return re.sub(r"\s+", " ", next(s for s in load_system_skills(SKILLS) if s.name == "drive-search").body)


def test_the_note_line_is_under_reading_the_results_once():
    body = _body()
    assert body.count(NOTE_LINE) == 1
    section = body[body.index("## Reading the results"):body.index("## Card accuracy")]
    assert NOTE_LINE in section


def test_card_accuracy_excepts_the_note():
    body = _body()
    section = body[body.index("## Card accuracy"):body.index("## When nothing is found")]
    assert ACCURACY in section
