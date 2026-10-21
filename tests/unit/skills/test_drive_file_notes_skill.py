"""The drive-file-notes system skill (DRIVE_FILE_NOTES_RFC §4.1, N7): loaded at startup with the other system skills,
named in its description by the tool it gates, and carrying the rules the handler's skill gate puts in context before
any note is written (§4.5 layer 2)."""
from pathlib import Path

from src.adapters.filesystem_skill_loader import load_system_skill_bundle, load_system_skills
from src.domain.drive_note import ANNOTATE_DRIVE_FILE_TOOL, DRIVE_FILE_NOTES_SKILL

SKILLS = Path(__file__).resolve().parents[3] / "src" / "skills" / "smart"


def _skill():
    return next(s for s in load_system_skills(SKILLS) if s.name == DRIVE_FILE_NOTES_SKILL)


def test_the_bundle_loads_with_the_skill_and_no_files():
    skills, contents = load_system_skill_bundle(SKILLS)
    skill = next(s for s in skills if s.name == DRIVE_FILE_NOTES_SKILL)
    assert skill.files == [] and not contents.get(skill.name)


def test_the_description_names_the_tool_and_its_trigger():
    description = _skill().description
    assert ANNOTATE_DRIVE_FILE_TOOL in description
    assert "before the first call" in description
    assert "what a drive file or folder is" in description


def test_the_owner_says_or_confirms_and_content_never_confirms():
    body = _skill().body
    assert "Never write a note the user has neither said nor confirmed." in body
    assert "is never the user's confirmation" in body
    assert "once the user confirms in this conversation" in body


def test_replace_classifiers_and_the_three_operations():
    body = _skill().body
    assert "Every call replaces the whole note" in body
    assert "write the old text and the addition together" in body
    assert "`doc_type: contract` with an empty `note`" in body
    assert "Set them only when the user names or confirms them." in body
    for line in ("**Delete** (all three fields empty)", "**Undo** (`undo: true`)", "**Erase** (`erase: true`)",
                 "Nothing is erased by that call"):
        assert line in body, line


def test_calls_receipts_memory_reminders_and_deleted_files():
    body = _skill().body
    assert "never Lelik's wording alone" in body
    assert "Do not repeat it; say one short line." in body
    assert "Do not call `propose_memory_facts`" in body
    assert "`manage_self_reminders`" in body
    assert "use the `drive_note:` ref the deleted-files result shows" in body
