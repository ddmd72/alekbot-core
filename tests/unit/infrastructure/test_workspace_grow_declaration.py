"""The workspace tool text for growing files (AGENT_WORKSPACE_RFC D18): declaration fields, token budget, the insert
command through the per-execution handler."""
import json

from src.domain.llm import ToolCall
from src.domain.workspace import COMMANDS, MUTATING_COMMANDS, WorkspaceReferences
from src.infrastructure import workspace_tools
from src.infrastructure.workspace_tools import build_workspace_tool_declaration, make_workspace_handler
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

# 5,346 chars before D18 (json.dumps defaults); the growth feature may add about 1,000.
DECLARATION_BUDGET = 6350
P = "projects/garden-shed.md"


def test_declaration_fits_the_budget():
    assert len(json.dumps(build_workspace_tool_declaration())) <= DECLARATION_BUDGET


def test_insert_is_a_declared_mutating_command():
    props = build_workspace_tool_declaration()["parameters"]["properties"]
    assert "insert" in COMMANDS and "insert" in MUTATING_COMMANDS
    assert "insert" in props["command"]["enum"] and "insert," in props["command"]["description"]
    assert props["insert_line"]["type"] == "integer" and props["insert_text"]["type"] == "string"
    assert "-1 = at the end" in props["insert_line"]["description"]
    assert "negative counts from the end" in props["offset"]["description"]
    for name in ("insert_line", "insert_text", "offset"):   # liberal schema: no limits, no unions
        assert not {"minimum", "maximum", "anyOf", "oneOf"} & set(props[name])


def test_description_teaches_growing_and_no_longer_says_keep_short():
    d = workspace_tools._DESCRIPTION
    assert "Files may grow: add new entries with insert at insert_line -1" in d
    assert "-50 = the last 50 lines" in d
    assert "do not append a log" not in d and "Large files are read in parts" not in d


def test_truncated_text_points_to_insert():
    assert workspace_tools._TRUNCATED.endswith(
        "nothing was written. Create the file with the first part, then add the rest with insert at insert_line -1.")


async def test_insert_runs_through_the_handler_and_is_recorded():
    store = InMemoryWorkspaceStore()
    store.seed("u1", P, "decision expected 2026-10-30\n")
    handle = make_workspace_handler(WorkspaceService(store), "u1", "chat", None, WorkspaceReferences())
    r = await handle(ToolCall(name="workspace", args={
        "command": "insert", "path": P, "insert_line": -1, "insert_text": "received 2026-10-28"}))
    assert not r.failed and r.result_str.startswith("inserted 1 line at line 2 of workspace:")
    # The recorded generation lets a middle insert follow without another view.
    r2 = await handle(ToolCall(name="workspace", args={
        "command": "insert", "path": P, "insert_line": 1, "insert_text": "note"}))
    assert not r2.failed
    assert store.live_text("u1", P) == "decision expected 2026-10-30\nnote\nreceived 2026-10-28\n"
