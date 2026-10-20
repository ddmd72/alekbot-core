"""draft_skill from_file accepts workspace:<path> (AGENT_WORKSPACE_RFC §11.2): read through FileConversionService
and the workspace port, with its own not-found message. The skill body is still passed inline."""
from unittest.mock import AsyncMock

import pytest

from src.domain.exceptions import SkillRejected
from src.domain.prompt_v3.security import RiskLevel, ValidationResult
from src.domain.skill import Skill, SkillFileChange
from src.infrastructure.skill_tools import build_draft_skill_tool_declaration
from src.ports.file_storage_port import FileStoragePort
from src.ports.security_port import SecurityPort
from src.ports.skill_repository import SkillRepository
from src.services.file_conversion_service import FileConversionService
from src.services.skill_service import SkillService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

NEW = Skill(name="fs", description="Use when x.", body="b")


def _passed(text, context, zone=None):
    return ValidationResult(sanitized_text=text, risk_level=RiskLevel.SAFE, risk_score=0.0, patterns_detected=[],
                            action_taken="passed", metadata={})


def _svc(store, storage=None):
    repo = AsyncMock(spec=SkillRepository)
    repo.list_current.return_value = []
    repo.create_draft.return_value = True
    security = AsyncMock(spec=SecurityPort)
    security.validate.side_effect = _passed
    conversion = FileConversionService(storage=storage or AsyncMock(spec=FileStoragePort), workspace=store)
    return SkillService(repository=repo, security_port=security, file_conversion=conversion), repo


async def test_a_scratch_draft_becomes_a_skill_file():
    store = InMemoryWorkspaceStore()
    store.seed("u1", "scratch/sources.md", "- source one\n- source two")
    svc, repo = _svc(store)
    res = await svc.draft("u1", NEW, [
        SkillFileChange(path="references/sources.md", from_file="workspace:scratch/sources.md")])
    assert list(repo.create_draft.call_args.kwargs["staged"].values()) == ["- source one\n- source two"]
    assert res.summary[0].source == "workspace:scratch/sources.md"


async def test_a_missing_workspace_file_has_its_own_message():
    svc, _ = _svc(InMemoryWorkspaceStore())
    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="r.md", from_file="workspace:scratch/nope.md")])
    assert str(e.value) == ("file 'workspace:scratch/nope.md' not found in your workspace — view the folder to "
                            "check the path")


async def test_the_upload_message_is_unchanged():
    storage = AsyncMock(spec=FileStoragePort)
    storage.download.side_effect = FileNotFoundError("rates.csv")
    svc, _ = _svc(InMemoryWorkspaceStore(), storage)
    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="r.csv", from_file="rates.csv")])
    assert "pass the bare filename from the file label" in str(e.value)


def test_the_from_file_description_names_workspace_refs():
    items = build_draft_skill_tool_declaration()["parameters"]["properties"]["files"]["items"]
    assert "'workspace:<path>'" in items["properties"]["from_file"]["description"]
