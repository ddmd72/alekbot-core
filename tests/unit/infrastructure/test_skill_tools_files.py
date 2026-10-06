from unittest.mock import AsyncMock

from src.domain.llm import ToolCall
from src.domain.skill import (
    SKILL_CONTEXT_KEY,
    DraftResult,
    FileChangeLine,
    Skill,
    SkillFile,
    SkillFileChange,
    body_marker,
    render_files_block,
    sha256_text,
)
from src.infrastructure.skill_tools import (
    build_draft_skill_tool_declaration,
    make_draft_skill_handler,
    make_use_skill_handler,
)


def _draft_call(**kwargs):
    return ToolCall(name="draft_skill", args=kwargs)


async def test_use_skill_appends_files_block_to_result_and_history():
    s = Skill(
        name="fs", description="Use when x.", body="Do it.", version=2,
        files=[SkillFile(path="references/a.md", sha256=sha256_text("hi"), size=2)],
    )

    r = await make_use_skill_handler([s], set())(ToolCall(name="use_skill", args={"name": "fs"}))

    assert "skill:fs/references/a.md" in r.result_str
    assert r.history_context[SKILL_CONTEXT_KEY]["body"].endswith(render_files_block(s))
    assert not r.failed


async def test_use_skill_without_files_unchanged():
    s = Skill(name="fs", description="Use when x.", body="Do it.", version=1)

    r = await make_use_skill_handler([s], set())(ToolCall(name="use_skill", args={"name": "fs"}))

    assert r.result_str == f"{body_marker('fs', 1)}\nDo it."
    assert r.history_context == {SKILL_CONTEXT_KEY: {"name": "fs", "version": 1, "body": "Do it."}}


def test_draft_declaration_has_optional_files_array():
    d = build_draft_skill_tool_declaration()

    assert d["parameters"]["required"] == ["name", "description", "body"]
    files = d["parameters"]["properties"]["files"]
    assert files["type"] == "array"
    item = files["items"]["properties"]
    assert set(item) == {"path", "content", "from_file", "remove"}
    assert "description" in files and all("description" in v for v in item.values())
    assert "required" not in files["items"]
    assert "additionalProperties" not in files["items"]
    assert "maxItems" not in files


async def test_draft_with_files_passes_changes_and_delivers_files_and_summary():
    line = FileChangeLine(kind="new", path="references/a.md", size=3)
    result = DraftResult(
        code="ab12", skill=Skill(name="fs", description="Use when x.", body="b"),
        model_files=[("references/a.md", "AAA")], summary=[line],
    )
    draft = AsyncMock(return_value=result)

    r = await make_draft_skill_handler(draft)(_draft_call(
        name="fs", description="Use when x.", body="b",
        files=[{"path": "references/a.md", "content": "AAA"}],
    ))

    changes = draft.call_args.args[1]
    assert changes == [SkillFileChange(path="references/a.md", content="AAA")]
    assert not r.failed
    data = r.delivery_items[0].data
    assert data["files"] == [{"path": "references/a.md", "content": "AAA"}]
    assert data["summary"] == [{"kind": "new", "path": "references/a.md", "size": 3, "source": None, "count": 0}]
    assert "ab12" not in r.result_str


async def test_draft_without_files_passes_empty_changes():
    result = DraftResult(code="ab12", skill=Skill(name="fs", description="Use when x.", body="b"))
    draft = AsyncMock(return_value=result)

    r = await make_draft_skill_handler(draft)(_draft_call(name="fs", description="Use when x.", body="b"))

    changes = draft.call_args.args[1]
    assert changes == []
    assert r.delivery_items[0].data["files"] == []
    assert r.delivery_items[0].data["summary"] == []


async def test_draft_invalid_files_entry_rejected_as_tool_failure():
    draft = AsyncMock()

    r = await make_draft_skill_handler(draft)(_draft_call(
        name="fs", description="Use when x.", body="b",
        files=[{"path": "../etc/passwd", "content": "x"}],
    ))

    assert r.failed
    assert "SYSTEM: draft rejected" in r.result_str
    assert r.delivery_items == []
    draft.assert_not_awaited()


async def test_draft_files_entry_with_two_actions_rejected():
    draft = AsyncMock()

    r = await make_draft_skill_handler(draft)(_draft_call(
        name="fs", description="Use when x.", body="b",
        files=[{"path": "references/a.md", "content": "x", "remove": True}],
    ))

    assert r.failed
    assert "SYSTEM: draft rejected" in r.result_str
    draft.assert_not_awaited()


async def test_draft_files_entry_not_a_dict_rejected():
    draft = AsyncMock()

    r = await make_draft_skill_handler(draft)(_draft_call(
        name="fs", description="Use when x.", body="b",
        files=[SkillFileChange(path="references/a.md", content="x")],
    ))

    assert r.failed
    assert "SYSTEM: draft rejected" in r.result_str
    draft.assert_not_awaited()


async def test_draft_files_not_a_list_rejected():
    draft = AsyncMock()

    r = await make_draft_skill_handler(draft)(_draft_call(
        name="fs", description="Use when x.", body="b", files="references/a.md",
    ))

    assert r.failed
    assert "SYSTEM: draft rejected" in r.result_str
    assert "files" in r.result_str
    draft.assert_not_awaited()


async def test_draft_accepts_null_remove_and_empty_from_file():
    result = DraftResult(code="ab12", skill=Skill(name="fs", description="Use when x.", body="b"))
    draft = AsyncMock(return_value=result)

    r = await make_draft_skill_handler(draft)(_draft_call(
        name="fs", description="Use when x.", body="b",
        files=[
            {"path": "references/a.md", "content": "AAA", "remove": None},
            {"path": "references/b.md", "content": "BBB", "from_file": ""},
        ],
    ))

    assert not r.failed
    assert draft.call_args.args[1] == [
        SkillFileChange(path="references/a.md", content="AAA"),
        SkillFileChange(path="references/b.md", content="BBB"),
    ]


async def test_draft_files_error_names_index_and_path():
    draft = AsyncMock()

    r = await make_draft_skill_handler(draft)(_draft_call(
        name="fs", description="Use when x.", body="b",
        files=[
            {"path": "references/a.md", "content": "AAA"},
            {"path": "references/b.md", "content": "x", "remove": True},
        ],
    ))

    assert r.failed
    assert "files[1] (references/b.md)" in r.result_str
    draft.assert_not_awaited()


async def test_draft_files_error_without_path_names_index():
    draft = AsyncMock()

    r = await make_draft_skill_handler(draft)(_draft_call(
        name="fs", description="Use when x.", body="b",
        files=[{"content": "AAA"}],
    ))

    assert r.failed
    assert "files[0]" in r.result_str
    draft.assert_not_awaited()
