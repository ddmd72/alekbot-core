"""System skills: read-only SKILL.md files shipped in git, loaded once at startup."""

from pathlib import Path
from typing import List

from ..domain.exceptions import SkillValidationError
from ..domain.skill import Skill
from ..utils.skill_md import parse_skill_md

SYSTEM_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills" / "smart"


def load_system_skills(root: Path) -> List[Skill]:
    """A malformed skill raises — it must fail startup, not disappear silently.

    A plain file at ``root`` (e.g. README.md) is ignored — only directories are
    candidate skills. A candidate directory without SKILL.md, a root that is
    missing or not a directory, and an empty result are all packaging bugs and
    all raise, same as a parse/name-mismatch failure.
    """
    if not root.is_dir():
        raise SkillValidationError(f"{root}: system skills root is not a directory")
    skills: List[Skill] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        skill_file = folder / "SKILL.md"
        if not skill_file.is_file():
            raise SkillValidationError(f"{folder}: missing SKILL.md")
        skill = parse_skill_md(skill_file.read_text(encoding="utf-8"))
        if skill.name != folder.name:
            raise SkillValidationError(f"{skill_file}: name '{skill.name}' must equal folder '{folder.name}'")
        skills.append(skill)
    if not skills:
        raise SkillValidationError(f"{root}: no system skills were loaded")
    return skills
