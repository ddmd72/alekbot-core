from src.infrastructure.skill_tools import build_use_skill_tool_declaration, make_use_skill_handler
from src.domain.llm import ToolCall
from src.domain.skill import Skill

S = Skill(name="flight-status", description="Use when x.", body="1. Open the page.", version=2)


def _call(name):
    return ToolCall(name="use_skill", args={"name": name})


def test_declaration_shape():
    d = build_use_skill_tool_declaration()
    assert d["name"] == "use_skill"
    assert d["parameters"]["required"] == ["name"]


async def test_returns_body_with_marker_and_history_context():
    r = await make_use_skill_handler([S], visible=set())(_call("flight-status"))
    assert r.result_str == '[Skill "flight-status" v2]\n1. Open the page.'
    assert r.history_context == {"skill_context": {"name": "flight-status", "version": 2, "body": "1. Open the page."}}
    assert not r.failed


async def test_visible_skill_is_not_sent_again():
    r = await make_use_skill_handler([S], visible={"flight-status"})(_call("flight-status"))
    assert "already in your context" in r.result_str
    assert r.history_context is None


async def test_second_load_in_same_execution_is_not_sent_again():
    handle = make_use_skill_handler([S], visible=set())
    await handle(_call("flight-status"))
    r = await handle(_call("flight-status"))
    assert "already in your context" in r.result_str


async def test_unknown_name_fails_with_available_list():
    r = await make_use_skill_handler([S], visible=set())(_call("flight-stat"))
    assert r.failed
    assert "flight-status" in r.result_str


async def test_handlers_do_not_share_state():
    make_use_skill_handler([S], visible=set())  # first execution
    r = await make_use_skill_handler([S], visible=set())(_call("flight-status"))
    assert r.history_context is not None  # a fresh execution loads again
