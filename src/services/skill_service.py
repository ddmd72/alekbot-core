"""Reads and saves skills — system (git, shared) and custom (per-user, Firestore).

Saving rejects flagged text — a skill is followed verbatim. Custom skills are drafted
by the model (`draft`) before the owner confirms them by code (`save_draft`), or saved
directly by the owner (`save`). A custom skill can shadow a system skill of the same
name, but a *new* custom skill cannot claim a system name (`SkillNameReserved`).
"""

import secrets
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from ..domain.exceptions import (
    SkillCapExceeded,
    SkillDraftNotFound,
    SkillFileMissing,
    SkillNameReserved,
    SkillRejected,
)
from ..domain.prompt_v3.security import TrustZone
from ..domain.skill import (
    MAX_CUSTOM_SKILLS_PER_USER,
    MAX_MODEL_FILES_PER_DRAFT,
    MAX_SKILL_FILE_BYTES,
    SKILL_FILE_EXTENSIONS,
    SKILL_REF_PREFIX,
    DraftResult,
    Skill,
    SkillFileChange,
    change_summary,
    is_delivered_ref,
    merge_manifest,
    merge_visible_skills,
    parse_skill_ref,
    sha256_text,
)
from ..ports.security_port import SecurityPort
from ..ports.skill_repository import SkillRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .file_conversion_service import FileConversionService

_MAX_DRAFT_CODE_ATTEMPTS = 5


class SkillService:

    def __init__(
        self,
        repository: SkillRepository,
        security_port: SecurityPort,
        system_skills: Sequence[Skill] = (),
        file_conversion: Optional["FileConversionService"] = None,
    ):
        self._repo = repository
        self._security = security_port
        self._system_skills = {s.name: s for s in system_skills}
        self._file_conversion = file_conversion

    def is_system(self, name: str) -> bool:
        return name in self._system_skills

    async def list_skills(self, user_id: str) -> List[Skill]:
        try:
            custom = await self._repo.list_current(user_id)
        except Exception as e:
            logger.error("❌ [Skills] list_current failed for user %s, falling back to system skills: %s",
                         user_id[:8], e)
            custom = []
        for skill in custom:
            if skill.name in self._system_skills:
                logger.warning("⚠️ [Skills] Custom skill %s shadows a system skill for user %s",
                               skill.name, user_id[:8])
        merged = merge_visible_skills(self._system_skills, custom)
        return sorted(merged.values(), key=lambda s: s.name)

    async def list_owned(self, user_id: str) -> Tuple[List[Skill], List[Skill]]:
        custom = await self._repo.list_current(user_id)
        system = sorted(self._system_skills.values(), key=lambda s: s.name)
        return custom, system

    async def _check(self, user_id: str, skill: Skill) -> None:
        if self.is_system(skill.name):
            raise SkillNameReserved(f"'{skill.name}' is a system skill name")
        for field, text in (("description", skill.description), ("body", skill.body)):
            await self._check_text(user_id, skill.name, field, text)

    async def _check_text(self, user_id: str, name: str, field: str, text: str) -> None:
        try:
            result = await self._security.validate(
                text, context=f"skill_{field}_user_{user_id}", zone=TrustZone.UNTRUSTED,
            )
        except ValueError as e:  # the regex/composite adapters raise on HIGH/CRITICAL
            logger.warning("🛡️ [Skills] Rejected skill %s: %s blocked (%s)", name, field, e)
            raise SkillRejected(f"{field} blocked by the security check: {e}") from e
        if result.action_taken != "passed":
            logger.warning(
                "🛡️ [Skills] Rejected skill %s: %s flagged %s",
                name, field, result.patterns_detected,
            )
            raise SkillRejected(f"{field} flagged by the security check: {result.patterns_detected}")

    async def save(self, user_id: str, account_id: str, skill: Skill) -> int:
        await self._check(user_id, skill)
        version = await self._repo.save_version(user_id, account_id, skill, cap=MAX_CUSTOM_SKILLS_PER_USER)
        logger.info("💾 [Skills] Saved %s v%s for user %s", skill.name, version, user_id[:8])
        return version

    async def draft(
        self, user_id: str, skill: Skill, changes: Sequence[SkillFileChange] = (),
    ) -> DraftResult:
        """Store a draft of `skill` with `changes` applied to its current files (RFC §15.5).

        Untouched files are inherited from the current version. Only content the current
        version does not already hold is staged with the draft.
        """
        await self._check(user_id, skill)
        model_written = [c for c in changes if c.content is not None]
        if len(model_written) > MAX_MODEL_FILES_PER_DRAFT:
            raise SkillRejected(
                f"at most {MAX_MODEL_FILES_PER_DRAFT} written files per draft — save in several drafts"
            )
        owned = await self._repo.list_current(user_id)  # one read: cap check + current files
        if skill.name not in {s.name for s in owned} and len(owned) >= MAX_CUSTOM_SKILLS_PER_USER:
            raise SkillCapExceeded(f"user already has {len(owned)} skills (cap {MAX_CUSTOM_SKILLS_PER_USER})")
        current = next((s for s in owned if s.name == skill.name), None)
        current_files = current.files if current else []

        resolved: List[Tuple[str, Optional[str]]] = []
        sources: Dict[str, str] = {}
        for c in changes:
            if c.remove:
                resolved.append((c.path, None))
            elif c.content is not None:
                resolved.append((c.path, c.content))
            else:
                resolved.append((c.path, await self._read_from_file(user_id, c.from_file, owned)))
                sources[c.path] = c.from_file
        try:
            manifest = merge_manifest(current_files, resolved)
            full = Skill(name=skill.name, description=skill.description, body=skill.body, files=manifest)
        except ValueError as e:
            logger.warning("🛡️ [Skills] Rejected draft of %s for user %s: %s", skill.name, user_id[:8], e)
            raise SkillRejected(str(e)) from e
        for path, content in resolved:
            if content is not None:
                await self._check_text(user_id, skill.name, f"file {path}", content)

        current_shas = {f.sha256 for f in current_files}
        staged: Dict[str, str] = {}
        for _, content in resolved:
            if content is not None:
                sha = sha256_text(content)
                if sha not in current_shas:
                    staged[sha] = content

        for _ in range(_MAX_DRAFT_CODE_ATTEMPTS):
            code = secrets.token_hex(2)
            if await self._repo.create_draft(user_id, code, full, staged=staged):
                logger.info("📝 [Skills] Drafted %s for user %s (%d files, %d staged)",
                            skill.name, user_id[:8], len(manifest), len(staged))
                return DraftResult(
                    code=code,
                    skill=full,
                    model_files=[(c.path, c.content) for c in model_written],
                    summary=change_summary(current_files, manifest, sources),
                )
        logger.error("❌ [Skills] Could not allocate a unique draft code for user %s after %d attempts",
                     user_id[:8], _MAX_DRAFT_CODE_ATTEMPTS)
        raise RuntimeError(f"could not allocate a draft code after {_MAX_DRAFT_CODE_ATTEMPTS} attempts")

    async def _read_from_file(self, user_id: str, ref: str, owned: Sequence[Skill]) -> str:
        """Read a `from_file` source verbatim as UTF-8 text (BOM and line endings kept)."""
        if is_delivered_ref(ref):
            raise SkillRejected("bot-delivered documents cannot be re-saved; write the needed part as a file")
        if ref.startswith(SKILL_REF_PREFIX):
            parsed = parse_skill_ref(ref)
            # Built-in skills are written only in git; only the owner's custom skills are copyable.
            if parsed is None or parsed[0] not in {s.name for s in owned}:
                raise SkillRejected("only files of your own skills can be re-saved")
        elif not ref.lower().endswith(SKILL_FILE_EXTENSIONS):
            raise SkillRejected(
                f"file {ref!r} is not a text file: {' '.join(SKILL_FILE_EXTENSIONS)}"
            )
        if self._file_conversion is None:
            raise SkillRejected("files cannot be re-saved here; write the needed part as a file")
        try:
            data = await self._file_conversion.resolve_bytes(ref, user_id)
        except (FileNotFoundError, PermissionError) as e:
            logger.warning("⚠️ [Skills] from_file %r not readable for user %s: %s", ref, user_id[:8], e)
            raise SkillRejected(f"file {ref!r} not found — pass the bare filename from the file label") from e
        if len(data) > MAX_SKILL_FILE_BYTES:
            raise SkillRejected(f"file {ref!r} is over {MAX_SKILL_FILE_BYTES // 1024} KB")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SkillRejected(f"file {ref!r} is not UTF-8 text") from e

    async def save_draft(self, user_id: str, account_id: str, code: str) -> Tuple[str, int]:
        skill = await self._repo.get_draft(user_id, code)
        if skill is None:
            raise SkillDraftNotFound(f"no pending draft {code!r} for user {user_id[:8]}")
        try:
            staged = await self._repo.get_draft_files(user_id, code)
        except SkillFileMissing as e:
            logger.warning("⚠️ [Skills] Draft %s of user %s lost a staged file: %s", skill.name, user_id[:8], e)
            raise SkillRejected(str(e)) from e
        await self._check(user_id, skill)
        if {sha256_text(t) for t in staged.values()} != set(staged):
            logger.error("❌ [Skills] Draft %s of user %s has staged content not matching its hash",
                         skill.name, user_id[:8])
            raise SkillRejected("draft files corrupted")
        for content in staged.values():
            await self._check_text(user_id, skill.name, "file", content)
        try:
            version = await self._repo.save_version(
                user_id, account_id, skill, cap=MAX_CUSTOM_SKILLS_PER_USER,
                consume_drafts_named=skill.name, draft_code=code,
            )
        except SkillFileMissing as e:
            logger.warning("⚠️ [Skills] Saving %s for user %s: a file is gone: %s", skill.name, user_id[:8], e)
            raise SkillRejected(str(e)) from e
        logger.info("💾 [Skills] Saved %s v%s for user %s (from draft)", skill.name, version, user_id[:8])
        return skill.name, version

    async def delete(self, user_id: str, name: str) -> bool:
        deleted = await self._repo.delete_skill(user_id, name)
        if not deleted and self.is_system(name):
            raise SkillNameReserved(f"'{name}' is a system skill and cannot be deleted")
        return deleted
