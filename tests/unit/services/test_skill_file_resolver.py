"""
Unit tests for SkillFileResolver (RFC §15.4).

Resolves `skill:<name>/<path>` refs to file content: custom skill (via SkillRepository)
shadows a system skill of the same name; system content is served from the in-memory
bundle the filesystem loader produced (Task 2), never from disk at request time.
"""

import pytest
from unittest.mock import AsyncMock

from src.domain.skill import Skill, SkillFile, sha256_text
from src.ports.skill_repository import SkillRepository
from src.services.skill_file_resolver import SkillFileResolver


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def repo():
    return AsyncMock(spec=SkillRepository)


def _skill(name="fs", path="r.md", content="hi", version=3) -> Skill:
    return Skill(
        name=name,
        description="Use when x.",
        body="b",
        files=[SkillFile(path=path, sha256=sha256_text(content), size=len(content.encode("utf-8")))],
        version=version,
    )


# ---------------------------------------------------------------------------
# Custom skill (repository-backed)
# ---------------------------------------------------------------------------

class TestCustomSkill:

    async def test_custom_current_version_file(self, repo):
        skill = _skill()
        repo.get_current.return_value = skill
        repo.get_file.return_value = "hi"
        r = SkillFileResolver(repo)

        assert await r.read("u1", "skill:fs/r.md") == "hi"

        repo.get_current.assert_awaited_once_with("u1", "fs")
        repo.get_file.assert_awaited_once_with("u1", "fs", sha256_text("hi"))

    async def test_missing_file_doc_raises_file_not_found(self, repo):
        """Manifest has the entry, but the stored content doc is gone."""
        skill = _skill()
        repo.get_current.return_value = skill
        repo.get_file.return_value = None
        r = SkillFileResolver(repo)

        with pytest.raises(FileNotFoundError):
            await r.read("u1", "skill:fs/r.md")


# ---------------------------------------------------------------------------
# System skill (served from the in-memory bundle)
# ---------------------------------------------------------------------------

class TestSystemSkill:

    async def test_system_skill_served_from_memory(self, repo):
        repo.get_current.return_value = None
        system_skill = _skill(name="sys-skill", path="ref.md", content="sys content")
        r = SkillFileResolver(
            repo,
            system_skills=[system_skill],
            system_contents={"sys-skill": {sha256_text("sys content"): "sys content"}},
        )

        result = await r.read("u1", "skill:sys-skill/ref.md")

        assert result == "sys content"
        repo.get_file.assert_not_awaited()

    async def test_custom_shadows_system(self, repo):
        """Same name on both sides → custom wins, content comes from the repo."""
        custom_skill = _skill(name="fs", path="r.md", content="custom content", version=2)
        system_skill = _skill(name="fs", path="r.md", content="system content", version=1)
        repo.get_current.return_value = custom_skill
        repo.get_file.return_value = "custom content"
        r = SkillFileResolver(
            repo,
            system_skills=[system_skill],
            system_contents={"fs": {sha256_text("system content"): "system content"}},
        )

        result = await r.read("u1", "skill:fs/r.md")

        assert result == "custom content"
        repo.get_file.assert_awaited_once_with("u1", "fs", sha256_text("custom content"))


# ---------------------------------------------------------------------------
# Not found / malformed
# ---------------------------------------------------------------------------

class TestNotFound:

    async def test_unknown_skill_raises_file_not_found(self, repo):
        repo.get_current.return_value = None
        r = SkillFileResolver(repo)

        with pytest.raises(FileNotFoundError) as exc_info:
            await r.read("u1", "skill:ghost/r.md")

        message = str(exc_info.value)
        assert "ghost" in message
        assert "re-upload" not in message

    async def test_unknown_path_raises_file_not_found(self, repo):
        skill = _skill()
        repo.get_current.return_value = skill
        r = SkillFileResolver(repo)

        with pytest.raises(FileNotFoundError) as exc_info:
            await r.read("u1", "skill:fs/nope.md")

        message = str(exc_info.value)
        assert "fs" in message
        assert "nope.md" in message
        assert "re-upload" not in message

    @pytest.mark.parametrize(
        "ref",
        [
            "report.docx",         # not a skill: ref at all
            "skill:",               # no name, no path
            "skill:fs",             # no path
            "skill:fs/",            # empty path
            "skill:UPPER/r.md",     # invalid name (parse_skill_ref rejects it)
        ],
    )
    async def test_malformed_ref_raises_file_not_found(self, repo, ref):
        r = SkillFileResolver(repo)

        with pytest.raises(FileNotFoundError):
            await r.read("u1", ref)

        repo.get_current.assert_not_awaited()


# ---------------------------------------------------------------------------
# User isolation
# ---------------------------------------------------------------------------

class TestUserIsolation:

    async def test_only_the_requesting_user_is_queried(self, repo):
        """The ref names no user — every repository call must carry the caller's own user_id,
        for both the custom-skill lookup and the file-content lookup."""
        skill = _skill()
        repo.get_current.return_value = skill
        repo.get_file.return_value = "hi"
        r = SkillFileResolver(repo)

        await r.read("u1", "skill:fs/r.md")

        for call in repo.get_current.await_args_list:
            assert call.args[0] == "u1"
        for call in repo.get_file.await_args_list:
            assert call.args[0] == "u1"
