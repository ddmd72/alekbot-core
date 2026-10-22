"""ConversationHandler: the `$drive-notes` erase commands (DRIVE_FILE_NOTES_RFC §4.8, D13), end to end over the real
DriveNoteService on the in-memory note store: `erase-all` answers with the count and a code and erases nothing;
pasting the code erases; a per-note code erases only that note; a wrong, expired, reused or someone else's code erases
nothing; a partial erase says how many could not be; the stub keeps no content; with indexing off the stubs go at once;
everything is handled before any LLM call."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.drive_note import (
    ONEDRIVE_PROVIDER_KEY, DriveNote, DriveNoteConflict, DriveNoteVersion, NoteBinding, NoteSnapshot, NoteState,
    NoteWriter, binding_key, ref_key,
)
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.conversation_handler import ConversationHandler
from src.locales import uk as uk_locale
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.embedding_service import EmbeddingService
from src.ports.ephemeral_store import EphemeralStore
from src.ports.security_port import SecurityPort
from src.ports.task_queue import TaskQueue
from src.ports.user_drive_port import UserDrivePort
from src.ports.user_repository import UserRepository
from src.services.drive_note_service import DriveNoteService

U, OTHER = "user-1", "user-2"
T = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def _ui(message: UIMessage, **fmt) -> str:
    template = uk_locale.UI_STRINGS[message.value]
    return template.format(**fmt) if fmt else template


class MemoryStore(EphemeralStore):
    """The erase-request store with a clock: an entry past its TTL reads as missing, as FirestoreEphemeralStore's."""

    def __init__(self, clock):
        self.clock, self.data = clock, {}

    async def set(self, key, value, ttl_s):
        self.data[key] = (value, self.clock() + timedelta(seconds=ttl_s))

    async def get(self, key) -> Optional[dict]:
        hit = self.data.get(key)
        return hit[0] if hit and hit[1] > self.clock() else None

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key) -> Optional[dict]:
        hit = self.data.pop(key, None)
        return hit[0] if hit and hit[1] > self.clock() else None


class Env:
    def __init__(self, *, indexing=True):
        self.now = T
        clock = lambda: self.now   # noqa: E731
        self.notes = InMemoryDriveNoteRepository(clock=clock)
        self.store = MemoryStore(clock)
        self.users = AsyncMock(spec=UserRepository)
        self.users.get_user.return_value = SimpleNamespace(config=SimpleNamespace(drive_indexing_enabled=indexing))
        self.drive = AsyncMock(spec=UserDrivePort)
        self.queue = AsyncMock(spec=TaskQueue)
        codes = iter(f"c0ffe{i}" for i in range(10))
        self.service = DriveNoteService(
            self.notes, self.drive, AsyncMock(spec=DriveIndexRepository), AsyncMock(spec=EmbeddingService),
            AsyncMock(spec=SecurityPort), self.users, embedding_space="s", nudge_queue=self.queue,
            erase_requests=self.store, clock=clock, new_code=lambda: next(codes))
        self.coordinator = MagicMock()
        self.handler = ConversationHandler(
            coordinator=self.coordinator, agent_factory=MagicMock(), file_service=MagicMock(),
            global_config=ConsolidationSettings(threshold=50, batch_size=30), drive_note_service=self.service)

    def put(self, n, user=U, versions=2):
        binding = NoteBinding(provider_key=ONEDRIVE_PROVIDER_KEY, drive_id="d1", item_id=f"i{n}")
        note = DriveNote(note_id=f"n{n}", user_id=user, account_id="acc", state=NoteState.ACTIVE,
                         current_version=versions, text=f"secret {n}", doc_type="contract", binding=binding,
                         snapshot=NoteSnapshot(name=f"f{n}.pdf", path=f"Docs/f{n}.pdf"),
                         match_keys=[binding_key(ONEDRIVE_PROVIDER_KEY, "d1", f"i{n}"), ref_key(f"i{n}")],
                         keys=["e:secret"], note_vector=[0.1], last_writer=NoteWriter(origin="chat"))
        self.notes.notes[(user, note.note_id)] = note
        self.notes.versions[(user, note.note_id)] = {
            v: DriveNoteVersion(version=v, text=f"secret {n}", valid_from=T) for v in range(1, versions + 1)}
        return note.note_id


def _context(text, user=U):
    return MessageContext(text=text, session_id=f"{user}:C1", user_id=user, account_id="acc", attachments=[],
                          metadata={})


def _channel():
    ch = MagicMock()
    ch.send_message = AsyncMock()
    ch.language = None
    return ch


def _sent(ch):
    return [c.args[0] for c in ch.send_message.await_args_list]


async def _run(env, command, user=U):
    ch = _channel()
    await env.handler.handle_command(command, _context(f"${command}", user), ch)
    return _sent(ch)


@pytest.fixture
def env():
    return Env()


class TestEraseAll:

    async def test_asks_with_the_count_and_a_code_and_erases_nothing(self, env):
        env.put(1)
        env.put(2)
        sent = await _run(env, "drive-notes erase-all")
        assert sent == [_ui(UIMessage.DRIVE_NOTE_ERASE_ALL_REQUEST, count=2, minutes=10),
                        "`$drive-notes erase-all c0ffe0`"]                   # part A's erase renderer
        assert all(not n.erase_pending for n in env.notes.notes.values())
        env.coordinator.route_message.assert_not_called()

    async def test_pasting_the_code_erases_every_note_and_its_history(self, env):
        ids = [env.put(1), env.put(2)]
        await _run(env, "drive-notes erase-all")
        sent = await _run(env, "drive-notes erase-all c0ffe0")
        assert sent == [_ui(UIMessage.DRIVE_NOTE_ERASED, count=2)]
        for nid in ids:
            assert env.notes.notes[(U, nid)].erase_pending and (U, nid) not in env.notes.versions

    async def test_no_notes(self, env):
        assert await _run(env, "drive-notes erase-all") == [_ui(UIMessage.DRIVE_NOTES_NONE)]


class TestOneNote:

    async def test_a_per_note_code_from_the_tool_erases_only_that_note(self, env):
        keep, gone = env.put(1), env.put(2)
        request = await env.service.request_erase(U, f"drive_note:{gone}")
        sent = await _run(env, f"drive-notes erase {request.code}")
        assert sent == [_ui(UIMessage.DRIVE_NOTE_ERASED, count=1)]
        assert env.notes.notes[(U, gone)].erase_pending and not env.notes.notes[(U, keep)].erase_pending

    async def test_the_stub_holds_no_content(self, env):
        nid = env.put(1)
        request = await env.service.request_erase(U, f"drive_note:{nid}")
        await _run(env, f"drive-notes erase {request.code}")
        stub = env.notes.notes[(U, nid)]
        assert (stub.text, stub.doc_type, stub.life_areas, stub.keys, stub.note_vector) == ("", "", [], [], None)
        assert stub.snapshot is None and stub.binding is None and stub.last_writer is None
        assert stub.state == NoteState.CLEARED.value and stub.match_keys and stub.account_id == "acc"

    async def test_the_bound_item_is_nudged_for_the_indexer(self, env):
        nid = env.put(1)
        request = await env.service.request_erase(U, f"drive_note:{nid}")
        await _run(env, f"drive-notes erase {request.code}")
        assert env.queue.enqueue_worker_task.await_count == 1

    async def test_with_indexing_off_the_stub_goes_at_once(self):
        env = Env(indexing=False)
        nid = env.put(1)
        request = await env.service.request_erase(U, f"drive_note:{nid}")
        await _run(env, f"drive-notes erase {request.code}")
        assert (U, nid) not in env.notes.notes


class TestCodes:

    async def test_a_wrong_code_erases_nothing(self, env):
        nid = env.put(1)
        await _run(env, "drive-notes erase-all")
        assert await _run(env, "drive-notes erase badbad") == [_ui(UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST)]
        assert not env.notes.notes[(U, nid)].erase_pending

    async def test_a_reused_code_erases_nothing_more(self, env):
        env.put(1)
        await _run(env, "drive-notes erase-all")
        await _run(env, "drive-notes erase-all c0ffe0")
        nid = env.put(2)
        assert await _run(env, "drive-notes erase-all c0ffe0") == [_ui(UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST)]
        assert not env.notes.notes[(U, nid)].erase_pending

    async def test_an_expired_code_erases_nothing(self, env):
        nid = env.put(1)
        await _run(env, "drive-notes erase-all")
        env.now = T + timedelta(minutes=11)
        assert await _run(env, "drive-notes erase-all c0ffe0") == [_ui(UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST)]
        assert not env.notes.notes[(U, nid)].erase_pending

    async def test_another_users_code_erases_nothing(self, env):
        mine = env.put(1)
        env.put(2, user=OTHER)
        await _run(env, "drive-notes erase-all", user=OTHER)
        assert await _run(env, "drive-notes erase-all c0ffe0") == [_ui(UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST)]
        assert not env.notes.notes[(U, mine)].erase_pending and not env.notes.notes[(OTHER, "n2")].erase_pending


class TestPartialAndFailures:

    async def test_a_partial_erase_says_how_many_could_not_be(self, env):
        env.put(1)
        stuck = env.put(2)
        real = env.notes._erase_one

        async def flaky(user_id, note_id):
            if note_id == stuck:
                raise DriveNoteConflict("changed")
            return await real(user_id, note_id)

        env.notes._erase_one = flaky
        await _run(env, "drive-notes erase-all")
        sent = await _run(env, "drive-notes erase-all c0ffe0")
        # final review C-I3: after an erase-all the owner sends the command again (its own variant)
        assert sent == [_ui(UIMessage.DRIVE_NOTE_ERASE_ALL_PARTIAL, erased=1, left=1)]

    async def test_an_unexpected_error_answers_unavailable(self, env):
        env.put(1)
        env.service.request_erase_all = AsyncMock(side_effect=RuntimeError("store down"))
        assert await _run(env, "drive-notes erase-all") == [_ui(UIMessage.DRIVE_NOTES_UNAVAILABLE)]

    async def test_without_the_service_notes_are_unavailable(self):
        handler = ConversationHandler(coordinator=MagicMock(), agent_factory=MagicMock(), file_service=MagicMock(),
                                      global_config=ConsolidationSettings(threshold=50, batch_size=30))
        ch = _channel()
        await handler.handle_command("drive-notes erase-all", _context("$drive-notes erase-all"), ch)
        assert _sent(ch) == [_ui(UIMessage.DRIVE_NOTES_UNAVAILABLE)]


class TestUsage:

    @pytest.mark.parametrize("command", ["drive-notes", "drive-notes erase", "drive-notes list", "drive-notes erase a b"])
    async def test_anything_else_prints_usage(self, env, command):
        assert await _run(env, command) == [_ui(UIMessage.DRIVE_NOTES_USAGE)]

    async def test_a_similar_command_is_not_taken(self, env):
        sent = await _run(env, "drive-notesx erase-all")
        assert sent == [_ui(UIMessage.UNKNOWN_COMMAND, command="drive-notesx erase-all")]

    async def test_every_language_has_the_strings(self):
        from src.locales import en, es, fr
        keys = [m.value for m in UIMessage if m.name.startswith("DRIVE_NOTES_")]
        assert sorted(keys) == ["drive_notes_no_such_request", "drive_notes_none", "drive_notes_unavailable",
                                "drive_notes_usage"]                     # C's four; the erase replies are A's
        for module in (uk_locale, en, es, fr):
            assert all(k in module.UI_STRINGS for k in keys)

    async def test_uk_and_fr_use_the_informal_register(self):
        """The feature speaks ти / tu (part A's receipts, e0dc71fc); none of C's strings says ви / vous."""
        from src.locales import fr
        keys = [m.value for m in UIMessage if m.name.startswith("DRIVE_NOTES_")]
        uk_text = " ".join(uk_locale.UI_STRINGS[k] for k in keys).lower()
        fr_text = " ".join(fr.UI_STRINGS[k] for k in keys).lower()
        for formal in ("ваш", "вставте", "попросіть", " вас", " вам"):
            assert formal not in uk_text
        assert "твої" in uk_text and "встав " in uk_text and "у тебе" in uk_text
        for formal in ("vous", " vos ", "collez", "votre"):
            assert formal not in fr_text
        assert "tes notes" in fr_text and "colle " in fr_text and "tu n'as" in fr_text
