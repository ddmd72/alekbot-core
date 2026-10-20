"""skill-creator teaches skills that keep state in a workspace file they describe themselves (AGENT_WORKSPACE_RFC
§11.2, D14, D16), and the user help names working files and reminders that carry one (§13)."""
from src.adapters.filesystem_skill_loader import SYSTEM_SKILLS_ROOT, load_system_skill_bundle
from src.utils.capabilities import CAPABILITIES_TEXT

_SKILL_MD = SYSTEM_SKILLS_ROOT / "skill-creator" / "SKILL.md"


def test_the_section_follows_the_files_section():
    text = _SKILL_MD.read_text(encoding="utf-8")
    assert (text.index("## Keep the body lean: files") < text.index("## Skills that keep state between runs")
            < text.index("## Hand it over"))


def test_the_section_text():
    text = _SKILL_MD.read_text(encoding="utf-8")
    for line in (
        "Some procedures carry state from one run to the next: a language tutor tracks the words learned and the "
        "mistakes that repeat; a weekly report compares with last week. Keep that state in a workspace file, not in "
        "the skill: the body is the procedure and changes only when the owner saves a new version; the file changes "
        "every run.",
        "- Name the file in the body with its full ref, describe what it holds, and say when to read and update it, "
        'e.g. "At the start, view `workspace:projects/spanish-tutor.md`. Before you finish, update its Learned and '
        'Repeating mistakes sections with str_replace."',
        "- Keep the file short and current: say in the body what to replace and what to drop, so it does not grow "
        "into a log.",
        "- If the file is missing, the view says whether it was deleted or renamed: tell the owner and offer to "
        "restore it. Create it only if it never existed.",
        '- Long reference files can be drafted in `scratch/` and passed as `from_file: "workspace:scratch/<name>.md"`.',
    ):
        assert line in text
    assert "reminder_skill" not in text and "state_file" not in text


def test_the_bundle_still_loads():
    skills, _ = load_system_skill_bundle(SYSTEM_SKILLS_ROOT)
    assert "skill-creator" in {s.name for s in skills}


def test_the_user_help_names_both():
    assert "*Working files*" in CAPABILITIES_TEXT
    assert "keep a file for the flat sale" in CAPABILITIES_TEXT
    assert "can also carry a workspace file" in CAPABILITIES_TEXT
