"""Final whole-branch review fixes for the Agent Workspace (AGENT_WORKSPACE_RFC §0.2, §6, §10, §16)."""
import logging
from unittest.mock import AsyncMock

import pytest

import src.domain.workspace as ws
from src.domain.llm import ToolCall
from src.domain.workspace import WorkspaceCall, WorkspaceRefused, WorkspaceReferences, parse_generation
from src.infrastructure.workspace_tools import build_workspace_tool_declaration, make_workspace_handler
from src.services.dry_run_workspace import DRY_RUN_ACCEPTED, DryRunWorkspace
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"


def _call(known=None):
    return WorkspaceCall(user_id=USER, origin="chat", known=dict(known or {}))


def _svc(conversion=None):
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store, conversion), store


def _conversion(data: bytes):
    conversion = AsyncMock()
    conversion.resolve_bytes = AsyncMock(return_value=data)
    return conversion


class TestCopySteer:
    def test_source_text_says_a_text_file_may_go_to_any_folder(self):
        text = build_workspace_tool_declaration()["parameters"]["properties"]["source"]["description"]
        assert "any folder" in text and "binary file only under files/" in text

    def test_wrong_store_message_points_to_copy_with_both_rules(self):
        with pytest.raises(WorkspaceRefused) as e:
            ws.parse_workspace_path("briefing.txt")
        assert str(e.value).endswith(
            'use copy with source "briefing.txt" and a destination path '
            "(a text file: any folder; a binary file: under files/)"
        )


class TestCopyGuards:
    async def test_drive_ref_anywhere_in_the_source_is_refused(self):
        svc, _ = _svc(_conversion(b"x"))
        r = await svc.run(_call(), "copy", {"source": "see drive:abc123", "path": "files/a.pdf"})
        assert r.failed and "on the user's drive" in r.text

    async def test_text_upload_over_256kb_into_projects_is_refused(self):
        svc, store = _svc(_conversion(b"a" * (256 * 1024 + 1)))
        r = await svc.run(_call(), "copy", {"source": "big.txt", "path": "projects/big.md"})
        assert r.failed and "text file over 256 KB" in r.text
        assert store.live_text(USER, "projects/big.md") is None

    async def test_overwrite_with_a_stale_known_generation_is_a_conflict(self):
        svc, store = _svc(_conversion(b"new"))
        stale = store.seed(USER, "projects/a.md", "old")
        store.seed(USER, "projects/a.md", "theirs")
        r = await svc.run(_call({"projects/a.md": stale.generation}), "copy",
                          {"source": "a.txt", "path": "projects/a.md"})
        assert r.failed and "changed since you read it" in r.text
        assert store.live_text(USER, "projects/a.md") == "theirs"


class TestByteCapOnOverwrite:
    async def test_copy_overwrite_refused_when_it_would_pass_the_cap(self, monkeypatch):
        monkeypatch.setattr(ws, "MAX_BYTES_PER_USER", 100)
        svc, store = _svc(_conversion(b"x" * 60))
        a = store.seed(USER, "projects/a.md", "a" * 50)
        store.seed(USER, "projects/b.md", "b" * 45)
        r = await svc.run(_call({"projects/a.md": a.generation}), "copy",
                          {"source": "u.txt", "path": "projects/a.md"})
        assert r.failed and r.text.startswith("SYSTEM: workspace full (")
        assert store.live_text(USER, "projects/a.md") == "a" * 50

    async def test_copy_overwrite_within_the_cap_is_allowed(self, monkeypatch):
        monkeypatch.setattr(ws, "MAX_BYTES_PER_USER", 100)
        svc, store = _svc(_conversion(b"x" * 60))
        a = store.seed(USER, "projects/a.md", "a" * 50)
        store.seed(USER, "projects/b.md", "b" * 30)
        r = await svc.run(_call({"projects/a.md": a.generation}), "copy",
                          {"source": "u.txt", "path": "projects/a.md"})
        assert not r.failed

    async def test_restore_over_a_live_file_refused_when_it_would_pass_the_cap(self, monkeypatch):
        monkeypatch.setattr(ws, "MAX_BYTES_PER_USER", 100)
        svc, store = _svc()
        old = store.seed(USER, "projects/a.md", "o" * 80)
        store.seed(USER, "projects/a.md", "n" * 10)
        store.seed(USER, "projects/b.md", "b" * 30)
        r = await svc.run(_call(), "restore", {"path": "projects/a.md", "generation": str(old.generation)})
        assert r.failed and r.text.startswith("SYSTEM: workspace full (")
        assert store.live_text(USER, "projects/a.md") == "n" * 10


class TestAsciiDigits:
    @pytest.mark.parametrize("raw", ["²", "①"])
    def test_parse_generation_rejects_non_ascii_digits(self, raw):
        with pytest.raises(WorkspaceRefused):
            parse_generation(raw)

    @pytest.mark.parametrize("raw", ["²", "①"])
    def test_view_window_treats_non_ascii_digits_as_default(self, raw):
        assert ws.view_window(raw, raw) == ws.view_window("", "")


class TestRefusalLogLevel:
    def _handler(self):
        return make_workspace_handler(WorkspaceService(InMemoryWorkspaceStore()), USER, "chat", None,
                                      WorkspaceReferences())

    async def test_truncated_logs_a_warning(self, caplog):
        with caplog.at_level(logging.INFO):
            await self._handler()(ToolCall(name="workspace", args={"_parse_error": "truncated_json"}))
        recs = [r for r in caplog.records if "refused:truncated" in r.getMessage()]
        assert recs and recs[0].levelno == logging.WARNING

    async def test_unknown_command_logs_info(self, caplog):
        with caplog.at_level(logging.INFO):
            await self._handler()(ToolCall(name="workspace", args={"command": "grep"}))
        recs = [r for r in caplog.records if "refused:command" in r.getMessage()]
        assert recs and recs[0].levelno == logging.INFO


class TestDryRunWorkspace:
    async def test_reads_pass_through_and_mutations_are_accepted_unwritten(self):
        svc, store = _svc()
        store.seed(USER, "projects/a.md", "hello")
        dry = DryRunWorkspace(svc)
        assert "hello" in (await dry.run(_call(), "view", {"path": "projects/a.md"})).text
        for command, args in [("create", {"path": "projects/n.md", "content": "x"}),
                              ("delete", {"path": "projects/a.md"}),
                              ("copy", {"source": "a.txt", "path": "projects/c.md"})]:
            assert (await dry.run(_call(), command, args)).text == DRY_RUN_ACCEPTED
        assert store.live_text(USER, "projects/n.md") is None
        assert store.live_text(USER, "projects/a.md") == "hello"

    def test_both_dry_run_scripts_install_the_wrapper(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[3] / "scripts" / "validation"
        for rel in ("ab_agent_models.py", "drive_eval/smart_dry_run.py"):
            src = (root / rel).read_text(encoding="utf-8")
            assert "DryRunWorkspace(" in src and "import DryRunWorkspace" in src, rel
