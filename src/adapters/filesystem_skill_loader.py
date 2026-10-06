"""System skills: read-only SKILL.md files (and their text files) shipped in git, loaded once
at startup."""

from pathlib import Path
from typing import Dict, List, Tuple

from ..domain.exceptions import SkillValidationError
from ..domain.skill import MAX_FILES_PER_SKILL, Skill, SkillFile, sha256_text
from ..utils.skill_md import parse_skill_md

SYSTEM_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills" / "smart"


def _load_files(folder: Path, skill_name: str) -> Tuple[List[SkillFile], Dict[str, str]]:
    manifest: List[SkillFile] = []
    contents: Dict[str, str] = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise SkillValidationError(f"{path}: symlinks are not allowed in a skill")
        rel_parts = path.relative_to(folder).parts
        if any(part.startswith(".") for part in rel_parts):
            continue  # .DS_Store, .gitkeep — never part of a skill
        if path.is_dir() or (path.name == "SKILL.md" and path.parent == folder):
            continue
        rel = path.relative_to(folder).as_posix()
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
            entry = SkillFile(path=rel, sha256=sha256_text(text), size=len(data))
        except (UnicodeDecodeError, ValueError) as e:  # pydantic ValidationError is a ValueError
            raise SkillValidationError(f"{path}: {e}") from e
        manifest.append(entry)
        contents[entry.sha256] = text
    if len(manifest) > MAX_FILES_PER_SKILL:
        raise SkillValidationError(f"{folder}: more than {MAX_FILES_PER_SKILL} files")
    return manifest, contents


def load_system_skill_bundle(root: Path) -> Tuple[List[Skill], Dict[str, Dict[str, str]]]:
    """A malformed skill raises — it must fail startup, not disappear silently.

    A plain file at ``root`` (e.g. README.md) is ignored — only directories are
    candidate skills. A candidate directory without SKILL.md, a root that is
    missing or not a directory, and an empty result are all packaging bugs and
    all raise, same as a parse/name-mismatch failure.

    Returns the skills (each with its ``files`` manifest populated) plus a bundle of
    skill name -> sha256 -> content for every text file found alongside its SKILL.md.
    """
    if not root.is_dir():
        raise SkillValidationError(f"{root}: system skills root is not a directory")
    skills: List[Skill] = []
    bundle_contents: Dict[str, Dict[str, str]] = {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        skill_file = folder / "SKILL.md"
        if not skill_file.is_file():
            raise SkillValidationError(f"{folder}: missing SKILL.md")
        skill = parse_skill_md(skill_file.read_text(encoding="utf-8"))
        if skill.name != folder.name:
            raise SkillValidationError(f"{skill_file}: name '{skill.name}' must equal folder '{folder.name}'")
        files, contents = _load_files(folder, skill.name)
        try:
            skill = Skill(name=skill.name, description=skill.description, body=skill.body, files=files)
        except ValueError as e:
            raise SkillValidationError(f"{skill_file}: {e}") from e
        bundle_contents[skill.name] = contents
        skills.append(skill)
    if not skills:
        raise SkillValidationError(f"{root}: no system skills were loaded")
    return skills, bundle_contents


def load_system_skills(root: Path) -> List[Skill]:
    return load_system_skill_bundle(root)[0]
