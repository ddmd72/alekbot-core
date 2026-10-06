from unittest.mock import AsyncMock

import pytest

from src.domain.agent import DeliveryItem
from src.domain.exceptions import SkillCapExceeded, SkillNameReserved, SkillRejected
from src.domain.llm import ToolCall
from src.domain.skill import SKILL_PREVIEW_DELIVERY, DraftResult, Skill, render_skill_md, save_command
from src.infrastructure.skill_tools import build_draft_skill_tool_declaration, make_draft_skill_handler


def _call(**kwargs):
    return ToolCall(name="draft_skill", args=kwargs)


def test_declaration_shape():
    d = build_draft_skill_tool_declaration()
    assert d["name"] == "draft_skill"
    assert d["parameters"]["required"] == ["name", "description", "body"]
    # Delivery C (RFC §15.5): an optional `files` property joins; it stays out of `required`.
    assert set(d["parameters"]["properties"]) == {"name", "description", "body", "files"}


async def test_success_delivers_preview_without_leaking_code_in_result_str():
    expected_skill = Skill(name="flight-status", description="Use when a flight is asked about.", body="1. Open.")
    draft = AsyncMock(return_value=DraftResult(code="ab12", skill=expected_skill))
    handle = make_draft_skill_handler(draft)

    r = await handle(_call(name="flight-status", description="Use when a flight is asked about.", body="1. Open."))

    assert not r.failed
    assert "ab12" not in r.result_str
    assert "$skill save" not in r.result_str
    assert len(r.delivery_items) == 1
    item = r.delivery_items[0]
    assert item.type == SKILL_PREVIEW_DELIVERY
    assert item.data["name"] == "flight-status"
    assert item.data["skill_md"] == render_skill_md(expected_skill)
    assert item.data["command"] == save_command("ab12")
    assert "ab12" in item.data["command"]

    draft.assert_awaited_once()
    passed_skill, passed_changes = draft.await_args.args
    assert isinstance(passed_skill, Skill)
    assert passed_skill == expected_skill
    assert passed_changes == []


async def test_validation_error_is_a_failed_result_without_calling_draft():
    draft = AsyncMock()
    handle = make_draft_skill_handler(draft)

    r = await handle(_call(name="Not Kebab Case", description="desc", body="body"))

    assert r.failed
    assert "SYSTEM: draft rejected" in r.result_str
    assert r.delivery_items == []
    draft.assert_not_awaited()


async def test_empty_body_is_a_validation_error():
    draft = AsyncMock()
    handle = make_draft_skill_handler(draft)

    r = await handle(_call(name="ok-name", description="desc", body="   "))

    assert r.failed
    assert "SYSTEM: draft rejected" in r.result_str
    draft.assert_not_awaited()


@pytest.mark.parametrize("exc", [SkillRejected("flagged text"), SkillNameReserved("reserved"), SkillCapExceeded("cap")])
async def test_service_rejection_is_a_failed_result_with_reason(exc):
    draft = AsyncMock(side_effect=exc)
    handle = make_draft_skill_handler(draft)

    r = await handle(_call(name="ok-name", description="desc", body="body"))

    assert r.failed
    assert "SYSTEM: draft rejected" in r.result_str
    assert str(exc) in r.result_str
    assert r.delivery_items == []


async def test_other_exception_is_a_generic_failed_result():
    draft = AsyncMock(side_effect=RuntimeError("firestore down"))
    handle = make_draft_skill_handler(draft)

    r = await handle(_call(name="ok-name", description="desc", body="body"))

    assert r.failed
    assert r.result_str == "SYSTEM: draft could not be stored."
    assert "firestore down" not in r.result_str
