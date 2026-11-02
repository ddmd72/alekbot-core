"""The workspace tool's grep and tree surface: declaration text, read-only handling, requirement pin
(AGENT_WORKSPACE_RFC §7.1–§7.2, §7.4)."""
import json
from pathlib import Path

from src.domain.llm import ToolCall
from src.domain.workspace import COMMANDS, MUTATING_COMMANDS, WorkspaceReferences
from src.infrastructure.workspace_tools import (
    WorkspaceSession,
    build_workspace_tool_declaration,
    make_workspace_handler,
)
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
_REQ = Path(__file__).resolve().parents[3] / "requirements.txt"


class TestDeclaration:
    def test_grep_is_a_command_and_read_only(self):
        assert "grep" in COMMANDS and "grep" not in MUTATING_COMMANDS
        assert "grep" in build_workspace_tool_declaration()["parameters"]["properties"]["command"]["enum"]

    def test_new_parameters_are_flat_and_each_names_its_command(self):
        props = build_workspace_tool_declaration()["parameters"]["properties"]
        types = {"depth": "integer", "pattern": "string", "context": "integer",
                 "ignore_case": "boolean", "files_only": "boolean", "regex": "boolean"}
        for name, kind in types.items():
            assert props[name]["type"] == kind
            assert props[name]["description"].startswith("grep:") or name == "depth"
        assert props["depth"]["description"].startswith("view")
        assert "grep" in props["path"]["description"]
        assert build_workspace_tool_declaration()["parameters"]["required"] == ["command"]

    def test_no_unions_or_schema_constraint_keywords(self):
        raw = json.dumps(build_workspace_tool_declaration())
        for banned in ("anyOf", "null", "maxItems", "maxLength", "minimum", "additionalProperties"):
            assert banned not in raw

    def test_description_teaches_tree_search_and_parts(self):
        d = build_workspace_tool_declaration()["description"]
        for needle in ('path "" shows the tree', "depth", "grep", "regex: true", "files_only", "offset", "says where it searched",
                       "read the latest with a negative offset (-50 = the last 50 lines)",
                       "Write a long document by creating it with the first part and adding the rest with insert -1"):
            assert needle in d


class TestHandler:
    async def _run(self, **args):
        store = InMemoryWorkspaceStore()
        store.seed(USER, "projects/garden-shed.md", "one\ndeadline\nthree")
        session = WorkspaceSession()
        handler = make_workspace_handler(WorkspaceService(store), USER, "chat", None, WorkspaceReferences(),
                                         session=session)
        result = await handler(ToolCall(name="workspace", args=args))
        return result, session

    async def test_grep_runs_without_session_record_or_pending_write(self):
        result, session = await self._run(command="grep", pattern="deadline", path="", context="", regex="false")
        assert not result.failed and "2:deadline" in result.result_str
        assert session.generations == {} and not session.pending
        assert not session.lock.locked()

    async def test_grep_failure_releases_the_lock(self):
        result, session = await self._run(command="grep", pattern="")
        assert result.failed and "needs a pattern" in result.result_str
        assert not session.lock.locked()

    async def test_view_empty_path_with_depth_filler(self):
        result, _ = await self._run(command="view", path="", depth="")
        assert result.result_str.splitlines()[:3] == [
            "workspace (all folders) — 1 file", "projects/ · 1 file", "  garden-shed.md · 18 B · 2026-10-20 · chat",
        ]


def test_re2_pin_is_present_and_not_loosened():
    lines = [line.split("#", 1)[0].strip() for line in _REQ.read_text().splitlines()]
    assert "google-re2>=1.1,<2" in lines
    assert "google-re2" not in lines
