"""System skills: read-only SKILL.md files shipped in git, loaded once at startup."""

from pathlib import Path
from typing import List

from ..domain.exceptions import SkillValidationError
from ..domain.skill import Skill
from ..utils.skill_md import parse_skill_md

SYSTEM_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills" / "smart"


def load_system_skills(root: Path) -> List[Skill]:
    """A malformed skill raises — it must fail startup, not disappear silently."""
    skills: List[Skill] = []
    if not root.is_dir():
        return skills
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        skill_file = folder / "SKILL.md"
        if not skill_file.is_file():
            continue
        skill = parse_skill_md(skill_file.read_text(encoding="utf-8"))
        if skill.name != folder.name:
            raise SkillValidationError(f"{skill_file}: name '{skill.name}' must equal folder '{folder.name}'")
        skills.append(skill)
    return skills
