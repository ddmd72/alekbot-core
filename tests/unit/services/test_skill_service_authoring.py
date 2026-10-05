from unittest.mock import AsyncMock

import pytest

from src.domain.exceptions import SkillCapExceeded, SkillDraftNotFound, SkillNameReserved, SkillRejected
from src.domain.prompt_v3.security import RiskLevel, TrustZone, ValidationResult
from src.domain.skill import MAX_CUSTOM_SKILLS_PER_USER, Skill
from src.ports.security_port import SecurityPort
from src.ports.skill_repository import SkillRepository
from src.services.skill_service import SkillService


def _result(text, action="passed"):
    return ValidationResult(sanitized_text=text, risk_level=RiskLevel.SAFE if action == "passed" else RiskLevel.HIGH,
                            risk_score=0.0, patterns_detected=[] if action == "passed" else ["ignore_previous"],
                            action_taken=action, metadata={})


@pytest.fixture
def repo():
    r = AsyncMock(spec=SkillRepository)
    r.save_version.return_value = 1
    r.list_current.return_value = []
    r.create_draft.return_value = True
    return r


@pytest.fixture
def security():
    s = AsyncMock(spec=SecurityPort)
    s.validate.side_effect = lambda text, context, zone=TrustZone.UNTRUSTED: _result(text)
    return s


SKILL = Skill(name="flight-status", description="Use when x.", body="1. Open.")
SYSTEM_SKILL = Skill(name="skill-creator", description="System skill.", body="System body.")


@pytest.fixture
def service(repo, security):
    return SkillService(repo, security, system_skills=[SYSTEM_SKILL])


# ---------------------------------------------------------------------------
# is_system
# ---------------------------------------------------------------------------

def test_is_system_true_for_system_name_false_otherwise(service):
    assert service.is_system("skill-creator") is True
    assert service.is_system("flight-status") is False


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------

async def test_list_skills_merges_system_and_custom_sorted(repo, service):
    repo.list_current.return_value = [SKILL]

    result = await service.list_skills("u1")

    assert [s.name for s in result] == ["flight-status", "skill-creator"]
    assert result[1] is SYSTEM_SKILL


async def test_list_skills_custom_shadows_system_name(repo, service):
    custom_override = Skill(name="skill-creator", description="Custom.", body="Custom body.")
    repo.list_current.return_value = [custom_override]

    result = await service.list_skills("u1")

    assert len(result) == 1
    assert result[0].body == "Custom body."


async def test_list_skills_repo_raising_returns_system_skills(repo, service):
    repo.list_current.side_effect = Exception("firestore outage")

    result = await service.list_skills("u1")

    assert result == [SYSTEM_SKILL]


# ---------------------------------------------------------------------------
# save (reserved name)
# ---------------------------------------------------------------------------

async def test_save_with_system_name_raises_reserved_and_does_not_save(repo, service):
    bad = Skill(name="skill-creator", description="Use when x.", body="1. Open.")

    with pytest.raises(SkillNameReserved):
        await service.save("u1", "a1", bad)

    repo.save_version.assert_not_awaited()


async def test_save_keeps_calling_save_version_with_cap_only(repo, service):
    await service.save("u1", "a1", SKILL)

    repo.save_version.assert_awaited_once_with("u1", "a1", SKILL, cap=MAX_CUSTOM_SKILLS_PER_USER)


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------

async def test_draft_returns_4_hex_lowercase_code_and_calls_create_draft(repo, service):
    code = await service.draft("u1", SKILL)

    assert len(code) == 4
    assert code == code.lower()
    assert all(c in "0123456789abcdef" for c in code)
    repo.create_draft.assert_awaited_once_with("u1", code, SKILL)


async def test_draft_collision_draws_a_second_different_code(repo, service):
    repo.create_draft.side_effect = [False, True]

    code = await service.draft("u1", SKILL)

    assert repo.create_draft.await_count == 2
    codes = [c.args[1] for c in repo.create_draft.await_args_list]
    assert codes[0] != codes[1]
    assert code == codes[1]


async def test_draft_flagged_text_raises_rejected_create_draft_not_awaited(repo, security, service):
    security.validate.side_effect = lambda text, context, zone=TrustZone.UNTRUSTED: _result(text, "sanitized")

    with pytest.raises(SkillRejected):
        await service.draft("u1", SKILL)

    repo.create_draft.assert_not_awaited()


async def test_draft_reserved_name_raises(repo, service):
    bad = Skill(name="skill-creator", description="Use when x.", body="1. Open.")

    with pytest.raises(SkillNameReserved):
        await service.draft("u1", bad)

    repo.create_draft.assert_not_awaited()


async def test_draft_new_name_at_cap_raises_cap_exceeded(repo, service):
    repo.list_current.return_value = [
        Skill(name=f"existing-{i}", description="d", body="b") for i in range(MAX_CUSTOM_SKILLS_PER_USER)
    ]

    with pytest.raises(SkillCapExceeded):
        await service.draft("u1", SKILL)

    repo.create_draft.assert_not_awaited()


async def test_draft_existing_name_at_cap_is_allowed(repo, service):
    repo.list_current.return_value = [SKILL] + [
        Skill(name=f"existing-{i}", description="d", body="b") for i in range(MAX_CUSTOM_SKILLS_PER_USER - 1)
    ]

    code = await service.draft("u1", SKILL)

    assert code
    repo.create_draft.assert_awaited_once()


async def test_draft_exhausts_five_attempts_then_raises_runtime_error(repo, service):
    repo.create_draft.return_value = False

    with pytest.raises(RuntimeError):
        await service.draft("u1", SKILL)

    assert repo.create_draft.await_count == 5


# ---------------------------------------------------------------------------
# save_draft
# ---------------------------------------------------------------------------

async def test_save_draft_not_found_raises(repo, service):
    repo.get_draft.return_value = None

    with pytest.raises(SkillDraftNotFound):
        await service.save_draft("u1", "a1", "abcd")


async def test_save_draft_happy_path_consumes_drafts_and_returns_name_version(repo, service):
    repo.get_draft.return_value = SKILL
    repo.save_version.return_value = 3

    result = await service.save_draft("u1", "a1", "abcd")

    assert result == (SKILL.name, 3)
    repo.save_version.assert_awaited_once_with(
        "u1", "a1", SKILL, cap=MAX_CUSTOM_SKILLS_PER_USER, consume_drafts_named=SKILL.name,
    )


async def test_save_draft_of_older_code_saves_that_drafts_own_content(repo, service):
    """Two drafts of the same name exist; the owner pastes the OLDER code. `save_draft` must
    pass exactly the content stored under THAT code to `save_version` (never the newer draft's)
    — that is the authorization the owner gave. Deleting the sibling (newer) draft by name is
    the repository's job inside `save_version`'s transaction, not SkillService's; this only
    asserts the `consume_drafts_named` argument SkillService hands it."""
    older_draft = Skill(name="flight-status", description="Use when x.", body="older body")
    repo.get_draft.return_value = older_draft
    repo.save_version.return_value = 1

    name, version = await service.save_draft("u1", "a1", "older-code")

    repo.get_draft.assert_awaited_once_with("u1", "older-code")
    repo.save_version.assert_awaited_once_with(
        "u1", "a1", older_draft, cap=MAX_CUSTOM_SKILLS_PER_USER, consume_drafts_named=older_draft.name,
    )
    assert name == older_draft.name
    assert version == 1


async def test_save_draft_flagged_at_save_raises_rejected_nothing_saved(repo, security, service):
    repo.get_draft.return_value = SKILL
    security.validate.side_effect = lambda text, context, zone=TrustZone.UNTRUSTED: _result(text, "sanitized")

    with pytest.raises(SkillRejected):
        await service.save_draft("u1", "a1", "abcd")

    repo.save_version.assert_not_awaited()


# ---------------------------------------------------------------------------
# list_owned
# ---------------------------------------------------------------------------

async def test_list_owned_returns_custom_and_system_separately(repo, service):
    repo.list_current.return_value = [SKILL]

    custom, system = await service.list_owned("u1")

    assert custom == [SKILL]
    assert system == [SYSTEM_SKILL]


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

async def test_delete_system_name_without_custom_copy_raises_reserved(repo, service):
    repo.delete_skill.return_value = False

    with pytest.raises(SkillNameReserved):
        await service.delete("u1", "skill-creator")


async def test_delete_shadowing_custom_copy_of_system_name_deletes(repo, service):
    repo.delete_skill.return_value = True

    result = await service.delete("u1", "skill-creator")

    assert result is True


async def test_delete_custom_name_returns_repo_result(repo, service):
    repo.delete_skill.return_value = False

    result = await service.delete("u1", "flight-status")

    assert result is False
