"""Read-only resolution of `skill:<name>/<path>` refs for the file pipeline (RFC §15.4)."""

from typing import Mapping, Optional, Sequence

from ..domain.skill import Skill, merge_visible_skills, parse_skill_ref
from ..ports.skill_repository import SkillRepository


class SkillFileResolver:
    """Resolves a `skill:` ref to the file's text content.

    Checks the user's custom skill first (it shadows a system skill of the same name,
    RFC §15.7); falls back to the system bundle's in-memory contents when the user has
    no custom skill of that name. Content for a custom skill always comes from the
    repository — `system_contents` never substitutes for a user's own files.
    """

    def __init__(
        self,
        repository: SkillRepository,
        system_skills: Sequence[Skill] = (),
        system_contents: Optional[Mapping[str, Mapping[str, str]]] = None,
    ):
        self._repo = repository
        self._system = {s.name: s for s in system_skills}
        self._system_contents = system_contents or {}

    async def read(self, user_id: str, ref: str) -> str:
        parsed = parse_skill_ref(ref)
        if parsed is None:
            raise FileNotFoundError(f"'{ref}' is not a skill file reference (skill:<name>/<path>)")
        name, path = parsed
        custom = await self._repo.get_current(user_id, name)
        skill = merge_visible_skills(self._system, [custom] if custom else []).get(name)
        if skill is None:
            raise FileNotFoundError(f"No skill named '{name}'")
        entry = next((f for f in skill.files if f.path == path), None)
        if entry is None:
            listed = ", ".join(f.path for f in skill.files) or "none"
            raise FileNotFoundError(f"Skill '{name}' has no file '{path}'. Its files: {listed}")
        if custom is not None:
            content = await self._repo.get_file(user_id, name, entry.sha256)
        else:
            content = self._system_contents.get(name, {}).get(entry.sha256)
        if content is None:
            raise FileNotFoundError(f"File '{path}' of skill '{name}' is missing from storage")
        return content
