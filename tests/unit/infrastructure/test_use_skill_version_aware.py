"""use_skill short-circuits only when the visible body is the current version (AGENT_WORKSPACE_RFC §11.2)."""
from src.domain.llm import ToolCall
from src.domain.skill import Skill
from src.infrastructure.skill_tools import make_use_skill_handler

S = Skill(name="flight-status", description="Use when x.", body="1. Open the page.", version=3)


def _call():
    return ToolCall(name="use_skill", args={"name": "flight-status"})


async def test_the_current_version_short_circuits():
    r = await make_use_skill_handler([S], visible={"flight-status": 3})(_call())
    assert "already in your context" in r.result_str and r.history_context is None


async def test_a_stale_visible_version_is_loaded_again():
    r = await make_use_skill_handler([S], visible={"flight-status": 2})(_call())
    assert r.result_str.startswith('[Skill "flight-status" v3]\n')
    assert r.history_context == {"skill_context": {"name": "flight-status", "version": 3, "body": "1. Open the page."}}


async def test_nothing_visible_loads():
    r = await make_use_skill_handler([S], visible={})(_call())
    assert r.history_context is not None
