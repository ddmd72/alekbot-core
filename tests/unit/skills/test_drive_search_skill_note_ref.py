"""The drive-search skill's deleted-files section names what a note's `drive_note:` ref is for (DRIVE_FILE_NOTES_RFC
D21, §6.4; plan part C default D-C16): it works only with `annotate_drive_file`, never to open a file."""
import re
from pathlib import Path

from src.adapters.filesystem_skill_loader import load_system_skills

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "src" / "skills" / "smart"
REF_LINE = ("A note on a deleted file carries a `drive_note:` ref, which works only with `annotate_drive_file` "
            "(to change, undo or remove that note).")


def _body() -> str:
    return re.sub(r"\s+", " ", next(s for s in load_system_skills(SKILLS) if s.name == "drive-search").body)


def test_the_note_ref_line_is_in_the_deleted_files_section_once():
    body = _body()
    assert body.count(REF_LINE) == 1
    section = body[body.index("## Deleted files"):]
    assert REF_LINE in section
