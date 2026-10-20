"""Smart's workspace tool: the declaration (AGENT_WORKSPACE_RFC §7.1–7.2) and the per-execution handler (§7.6):
lock first, shielded mutations that record and log themselves, one session per execution (§10 rows 2, 3, 11)."""
import asyncio
import json
import logging

import pytest

from src.domain.llm import ToolCall
from src.domain.workspace import COMMANDS, WorkspaceReferences
from src.infrastructure.workspace_tools import (
    WorkspaceSession,
    build_workspace_tool_declaration,
    make_workspace_handler,
)
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
P = "projects/a.md"
Q = "projects/b.md"
F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"


def _call(**args):
    return ToolCall(name="workspace", args=args)


def _handler(store, session=None, origin="chat", fire=None):
    return make_workspace_handler(WorkspaceService(store), USER, origin, fire, WorkspaceReferences(), session=session)


async def _until(condition):
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


class TestDeclaration:
    def test_shape(self):
        d = build_workspace_tool_declaration()
        assert d["name"] == "workspace"
        params = d["parameters"]
        assert params["required"] == ["command"]
        assert params["properties"]["command"]["enum"] == list(COMMANDS)
        assert set(params["properties"]) == {
            "command", "path", "offset", "limit", "generation", "content", "old_str", "new_str", "new_path", "source",
        }
        assert all(p.get("description") for p in params["properties"].values())
        assert params["properties"]["offset"]["type"] == params["properties"]["limit"]["type"] == "integer"
        assert params["properties"]["generation"]["type"] == "string"

    def test_no_null_unions_or_advertised_constraints(self):
        raw = json.dumps(build_workspace_tool_declaration())
        for banned in ("anyOf", "null", "maxItems", "pattern", "maxLength", "minimum", "additionalProperties"):
            assert banned not in raw

    def test_description_is_the_rfc_text(self):
        """Byte-identical to AGENT_WORKSPACE_RFC §7.1 (the blockquote, lines joined with single spaces)."""
        assert build_workspace_tool_declaration()["description"] == (
            "Your workspace: files you keep for your own work between conversations and runs — a knowledge base you "
            "build and follow. Use it when the user asks you to, when an instruction you are following (a skill, a "
            "reminder) names a workspace file or asks you to keep state, or when a firing reminder carries a "
            "workspace file; not on your own initiative otherwise. How to use a file is up to that instruction; when "
            'it says nothing, use your judgement. "Keep state in the workspace" means: keep one file for it, view it '
            "at the start, update it before you finish. Folders: projects/ (one file per task or recurring job), "
            "references/ (where things live and how to find them), scratch/ (drafts; deleted 7 days after the last "
            "write), files/ (byte-exact copies of chat uploads or delivered documents, made with copy; look at one "
            "with open_file and the ref workspace:files/<name>). Keep files short and current: replace what changed "
            "with str_replace, do not append a log. Facts about the user never go here: what the user says reaches "
            "memory by itself; facts you read in a file or other source go through propose_memory_facts, only when "
            "the user asks. Something to do at a given time is a reminder. Every change keeps the previous version: "
            "history shows who wrote each one, restore brings one back. If a view header says an earlier attempt of "
            "this same reminder fire wrote the file, its result may not have reached the user: view the generation "
            "it names and work from that. If a file a skill or reminder names is missing, view shows whether it was "
            "deleted or renamed: tell the user and offer to restore it; never recreate it silently. A file that "
            "never existed may be created."
        )


class TestSession:
    async def test_view_then_create_overwrites_in_one_execution(self):
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "old")
        handle = _handler(store)
        await handle(_call(command="view", path=P))
        r = await handle(_call(command="create", path=P, content="new"))
        assert not r.failed and store.live_text(USER, P) == "new"

    async def test_create_over_an_existing_file_without_a_view_is_refused(self):
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "old")
        r = await _handler(store)(_call(command="create", path=P, content="new"))
        assert r.failed and "exists. View it first" in r.result_str

    async def test_a_fresh_handler_has_a_fresh_session(self):
        """Provider rotation builds a new handler: the failed attempt's views do not count (§10 row 11)."""
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "old")
        await _handler(store)(_call(command="view", path=P))
        r = await _handler(store)(_call(command="create", path=P, content="new"))
        assert r.failed and store.live_text(USER, P) == "old"

    async def test_origin_and_fire_are_stamped(self):
        store = InMemoryWorkspaceStore()
        await _handler(store, origin="background:execute_reminder", fire=F)(
            _call(command="create", path=P, content="x"))
        [e] = store.versions_of(USER, P)
        assert (e.origin, e.fire) == ("background:execute_reminder", F)

    async def test_no_history_context(self):
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "x")
        r = await _handler(store)(_call(command="view", path=P))
        assert r.history_context is None and r.delivery_items == [] and r.name == "workspace"


class TestBatchOrder:
    async def test_batch_applies_in_emission_order(self):
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "A: 1\nB: 2")
        store.calls.clear()
        handle = _handler(store)
        results = await asyncio.gather(
            handle(_call(command="view", path=P)),
            handle(_call(command="str_replace", path=P, old_str="A: 1", new_str="A: 2")),
            handle(_call(command="str_replace", path=P, old_str="B: 2", new_str="B: 3")),
        )
        assert [r.failed for r in results] == [False, False, False]
        assert store.live_text(USER, P) == "A: 2\nB: 3"
        assert store.calls == [("read", P), ("read", P), ("put", P), ("read", P), ("put", P)]

    async def test_view_and_create_in_one_batch(self):
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "old")
        handle = _handler(store)
        _, created = await asyncio.gather(
            handle(_call(command="view", path=P)), handle(_call(command="create", path=P, content="new")),
        )
        assert not created.failed and store.live_text(USER, P) == "new"


class TestRefusalsAndLogs:
    async def test_unknown_command_is_refused_and_releases_the_lock(self, caplog):
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "x")
        handle = _handler(store)
        with caplog.at_level(logging.INFO):
            r = await handle(_call(command="grep", path=P))
        assert r.failed and r.result_str.startswith("SYSTEM: unknown workspace command 'grep'; use one of: view,")
        assert "result=refused:command" in caplog.text
        assert not (await handle(_call(command="view", path=P))).failed

    async def test_truncated_arguments_are_refused_and_write_nothing(self, caplog):
        store = InMemoryWorkspaceStore()
        handle = _handler(store)
        with caplog.at_level(logging.INFO):
            r = await handle(ToolCall(name="workspace",
                                      args={"_parse_error": "truncated_json", "_raw_prefix": '{"command": "create'}))
        assert r.failed and r.result_str == (
            "SYSTEM: your arguments were cut off at the output limit; nothing was written. Create a short file, "
            "then fill sections with str_replace.")
        assert store.calls == [] and "result=refused:truncated" in caplog.text
        assert not (await handle(_call(command="view", path=P))).failed  # lock released

    async def test_failed_mutation_logs_a_warning(self, caplog):
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "x")
        with caplog.at_level(logging.INFO):
            await _handler(store)(_call(command="create", path=P, content="y"))
        assert any(rec.levelno == logging.WARNING and "op=create" in rec.getMessage()
                   and "result=refused:exists" in rec.getMessage() for rec in caplog.records)


class TestShield:
    """A cancelled caller never loses a mutation: both rename steps land, the session map and the log line are
    written by the shielded call itself (§7.6, §10 row 3)."""

    @staticmethod
    def _setup():
        store = InMemoryWorkspaceStore()
        store.seed(USER, P, "state")
        store.delete_gate = asyncio.Event()
        session = WorkspaceSession()
        return store, session, _handler(store, session=session)

    @staticmethod
    def _landed(store, session):
        assert store.live_text(USER, Q) == "state" and store.live_text(USER, P) is None
        assert session.generations == {Q: store.versions_of(USER, Q)[-1].generation}

    async def test_cancelled_caller(self, caplog):
        store, session, handle = self._setup()
        task = asyncio.ensure_future(handle(_call(command="rename", path=P, new_path=Q)))
        await _until(lambda: ("delete", P) in store.calls)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with caplog.at_level(logging.INFO):
            store.delete_gate.set()
            await asyncio.gather(*list(session.pending))
        self._landed(store, session)
        assert any("op=rename" in r.getMessage() and "result=ok" in r.getMessage() for r in caplog.records)

    async def test_turn_budget_cut(self):
        """DelegationEngine wraps the whole batch in asyncio.wait_for (delegation_engine.py ~414-426)."""
        store, session, handle = self._setup()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(handle(_call(command="rename", path=P, new_path=Q))), timeout=0.05)
        store.delete_gate.set()
        await asyncio.gather(*list(session.pending))
        self._landed(store, session)

    async def test_message_timeout(self):
        """BaseAgent wraps execute in asyncio.wait_for on the message timeout (base_agent.py ~682-695)."""
        store, session, handle = self._setup()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(handle(_call(command="rename", path=P, new_path=Q)), timeout=0.05)
        store.delete_gate.set()
        await asyncio.gather(*list(session.pending))
        self._landed(store, session)

    async def test_the_lock_is_held_until_the_mutation_lands(self):
        store, session, handle = self._setup()
        task = asyncio.ensure_future(handle(_call(command="rename", path=P, new_path=Q)))
        await _until(lambda: ("delete", P) in store.calls)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        view = asyncio.ensure_future(handle(_call(command="view", path=P)))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not view.done()
        store.delete_gate.set()
        r = await view
        assert f"It was renamed to {Q}" in r.result_str
