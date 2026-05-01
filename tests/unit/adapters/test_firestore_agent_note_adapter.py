"""
Wire tests for FirestoreAgentNoteAdapter.

Mock at the Firestore SDK boundary (db_client), NOT at the port level.
Per ADAPTER_WIRE_TESTING.md mandate.

Tests cover:
- create_note: generates epoch-ms note_id; validates word count; enforces note cap
- list_active_notes: filters visible_after > as_of and expires_after <= as_of
- delete_note: not found → False; ownership mismatch → False; found → True
- update_note: not found raises ValueError; ownership mismatch raises ValueError; updates fields
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.domain.agent_note import AgentNote, NoteCreate, NoteUpdate
from src.config.environment import EnvironmentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_USER_ID = "user-abc123"
_OTHER_USER_ID = "user-other"
_NOW = datetime(2026, 3, 9, 14, 30, 22, tzinfo=timezone.utc)
_FUTURE = _NOW + timedelta(hours=1)
_PAST = _NOW - timedelta(hours=1)
_NOTE_ID = str(int(_NOW.timestamp() * 1000))  # epoch ms
_DEFAULT_INSTRUCTION = "Call the dentist and schedule an appointment"


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.orchestrator_notes_collection = "test_orchestrator_notes"
    return cfg


@pytest.fixture
def col_mock():
    """Mock Firestore collection."""
    return MagicMock()


@pytest.fixture
def db_mock(col_mock):
    """Mock Firestore db_client. collection() returns col_mock."""
    db = MagicMock()
    db.collection.return_value = col_mock
    return db


@pytest.fixture
def adapter(db_mock, env_config):
    return FirestoreAgentNoteAdapter(db_mock, env_config)


def _make_doc_snapshot(note_id: str, data: dict, exists: bool = True) -> MagicMock:
    doc = MagicMock()
    doc.exists = exists
    doc.id = note_id
    doc.to_dict.return_value = data
    return doc


def _make_note_data(
    user_id: str = _USER_ID,
    text: str = "Remind about dentist",
    instruction: str = _DEFAULT_INSTRUCTION,
    created_at: datetime = _NOW,
    due: datetime = None,
) -> dict:
    return {
        "user_id": user_id,
        "text": text,
        "instruction": instruction,
        "created_at": created_at,
        "due": due if due is not None else _FUTURE,
    }


# ---------------------------------------------------------------------------
# create_note tests
# ---------------------------------------------------------------------------


class TestCreateNote:

    async def test_create_note_generates_epoch_ms_id(self, adapter, col_mock):
        """note_id is a 13-digit epoch-milliseconds string."""
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref

        # list_active_notes (called inside create_note) returns empty list
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        before_ms = int(_NOW.timestamp() * 1000)
        with patch("src.adapters.firestore_agent_note_adapter.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.now.side_effect = None
            mock_dt.side_effect = None

            data = NoteCreate(user_id=_USER_ID, text="Remind about dentist", instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
            note = await adapter.create_note(data)

        assert len(note.note_id) == 13
        assert note.note_id.isdigit()
        note_id_int = int(note.note_id)
        assert abs(note_id_int - before_ms) < 5000  # within 5 seconds

    async def test_create_note_persists_to_firestore(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        data = NoteCreate(user_id=_USER_ID, text="Check tomorrow", instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        note = await adapter.create_note(data)

        col_mock.document.assert_called_once_with(note.note_id)
        doc_ref.set.assert_called_once()
        saved = doc_ref.set.call_args[0][0]
        assert saved["user_id"] == _USER_ID
        assert saved["text"] == "Check tomorrow"

    async def test_create_note_word_count_exactly_25_passes(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        text = " ".join(["word"] * 25)
        data = NoteCreate(user_id=_USER_ID, text=text, instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        note = await adapter.create_note(data)
        assert note.text == text

    async def test_create_note_word_count_exceeds_limit_raises(self, adapter, col_mock):
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        data = NoteCreate(user_id=_USER_ID, text=" ".join(["word"] * 26), instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        with pytest.raises(ValueError, match="25 words"):
            await adapter.create_note(data)

    async def test_create_note_at_cap_raises(self, adapter, col_mock):
        """30 active notes → ValueError before writing."""
        # Use a far-future due so notes survive the due > as_of filter in list_active_notes
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        active_docs = [
            _make_doc_snapshot(
                f"note{i}",
                _make_note_data(text=f"Note {i}", due=far_future),
            )
            for i in range(30)
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=active_docs)

        data = NoteCreate(user_id=_USER_ID, text="One more note", instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        with pytest.raises(ValueError, match="cap"):
            await adapter.create_note(data)

    async def test_create_note_returns_agent_note(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        data = NoteCreate(user_id=_USER_ID, text="Buy groceries", instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        note = await adapter.create_note(data)

        assert isinstance(note, AgentNote)
        assert note.user_id == _USER_ID
        assert note.text == "Buy groceries"


# ---------------------------------------------------------------------------
# list_active_notes tests
# ---------------------------------------------------------------------------


class TestListActiveNotes:

    async def test_returns_notes_with_future_due(self, adapter, col_mock):
        """Notes with due > as_of are included."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Note A", due=_FUTURE)),
            _make_doc_snapshot("n2", _make_note_data(text="Note B", due=_FUTURE)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 2

    async def test_excludes_note_with_past_due(self, adapter, col_mock):
        """Notes with due <= as_of are excluded (already fired or overdue)."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Overdue", due=_PAST)),
            _make_doc_snapshot("n2", _make_note_data(text="Active", due=_FUTURE)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 1
        assert notes[0].text == "Active"

    async def test_excludes_note_with_due_equal_to_as_of(self, adapter, col_mock):
        """Notes with due == as_of are excluded (fire moment has passed)."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Just fired", due=_NOW)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 0

    async def test_includes_note_with_future_due(self, adapter, col_mock):
        """Notes with due > as_of are included regardless of how far in future."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Far future", due=_NOW + timedelta(days=30))),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 1

    async def test_all_overdue_returns_empty(self, adapter, col_mock):
        """All notes past due → empty list."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Expired A", due=_PAST)),
            _make_doc_snapshot("n2", _make_note_data(text="Expired B", due=_PAST)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 0

    async def test_sorted_by_created_at_ascending(self, adapter, col_mock):
        t1 = datetime(2026, 3, 9, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)
        docs = [
            _make_doc_snapshot("n2", _make_note_data(text="Later", created_at=t2)),
            _make_doc_snapshot("n1", _make_note_data(text="Earlier", created_at=t1)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert notes[0].text == "Earlier"
        assert notes[1].text == "Later"

    async def test_empty_when_no_notes(self, adapter, col_mock):
        col_mock.where.return_value.get = AsyncMock(return_value=[])
        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert notes == []


# ---------------------------------------------------------------------------
# delete_note tests
# ---------------------------------------------------------------------------


class TestDeleteNote:

    async def test_delete_not_found_returns_false(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(_NOTE_ID, {}, exists=False))
        col_mock.document.return_value = doc_ref

        result = await adapter.delete_note(_NOTE_ID, _USER_ID)
        assert result is False

    async def test_delete_ownership_mismatch_returns_false(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data(user_id=_OTHER_USER_ID)
        ))
        col_mock.document.return_value = doc_ref

        result = await adapter.delete_note(_NOTE_ID, _USER_ID)
        assert result is False
        doc_ref.delete.assert_not_called()

    async def test_delete_success_returns_true(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.delete = AsyncMock()
        col_mock.document.return_value = doc_ref

        result = await adapter.delete_note(_NOTE_ID, _USER_ID)
        assert result is True
        doc_ref.delete.assert_called_once()


# ---------------------------------------------------------------------------
# update_note tests
# ---------------------------------------------------------------------------


class TestUpdateNote:

    async def test_update_not_found_raises(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(_NOTE_ID, {}, exists=False))
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, text="New text")
        with pytest.raises(ValueError, match="not found"):
            await adapter.update_note(upd)

    async def test_update_ownership_mismatch_raises(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data(user_id=_OTHER_USER_ID)
        ))
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, text="New text")
        with pytest.raises(ValueError, match="does not belong"):
            await adapter.update_note(upd)

    async def test_update_text_success(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, text="Updated text")
        note = await adapter.update_note(upd)

        doc_ref.update.assert_called_once_with({"text": "Updated text"})
        assert note.text == "Updated text"
        assert note.note_id == _NOTE_ID

    async def test_update_text_word_count_exceeds_raises(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, text=" ".join(["w"] * 26))
        with pytest.raises(ValueError, match="25 words"):
            await adapter.update_note(upd)

    async def test_update_no_fields_skips_firestore_call(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID)
        note = await adapter.update_note(upd)

        doc_ref.update.assert_not_called()
        assert isinstance(note, AgentNote)

    async def test_update_instruction_only(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, instruction="New instruction")
        note = await adapter.update_note(upd)

        doc_ref.update.assert_called_once_with({"instruction": "New instruction"})
        assert note.instruction == "New instruction"

    async def test_update_due_only(self, adapter, col_mock):
        new_due = _FUTURE + timedelta(hours=2)
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, due=new_due)
        note = await adapter.update_note(upd)

        doc_ref.update.assert_called_once_with({"due": new_due})
        assert note.due == new_due

    async def test_update_recurrence_serialised_to_dict(self, adapter, col_mock):
        from src.domain.agent_note import ReminderRecurrence
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        recurrence = ReminderRecurrence(type="weekly", interval=2)
        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, recurrence=recurrence)
        await adapter.update_note(upd)

        doc_ref.update.assert_called_once_with(
            {"recurrence": {"type": "weekly", "interval": 2}}
        )

    async def test_update_complexity_serialised_to_value(self, adapter, col_mock):
        from src.domain.task_complexity import TaskComplexity
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, complexity=TaskComplexity.DEEP_REASONING)
        await adapter.update_note(upd)

        doc_ref.update.assert_called_once_with({"complexity": "deep_reasoning"})


# ---------------------------------------------------------------------------
# get_note tests
# ---------------------------------------------------------------------------


class TestGetNote:

    async def test_returns_note_when_exists_and_owned(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        col_mock.document.return_value = doc_ref

        result = await adapter.get_note(user_id=_USER_ID, note_id=_NOTE_ID)

        assert result is not None
        assert result.note_id == _NOTE_ID
        assert result.user_id == _USER_ID

    async def test_returns_none_when_doc_missing(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, {}, exists=False,
        ))
        col_mock.document.return_value = doc_ref

        result = await adapter.get_note(user_id=_USER_ID, note_id=_NOTE_ID)

        assert result is None

    async def test_returns_none_on_ownership_mismatch(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data(user_id=_OTHER_USER_ID)
        ))
        col_mock.document.return_value = doc_ref

        result = await adapter.get_note(user_id=_USER_ID, note_id=_NOTE_ID)

        assert result is None

    async def test_returns_none_for_empty_note_id(self, adapter, col_mock):
        result = await adapter.get_note(user_id=_USER_ID, note_id="")

        assert result is None
        # No collection access performed when note_id is falsy.
        col_mock.document.assert_not_called()


# ---------------------------------------------------------------------------
# reschedule_if_due_at tests (atomic conditional reschedule)
# ---------------------------------------------------------------------------


class TestRescheduleIfDueAt:
    """Atomic conditional reschedule via Firestore transaction.

    Tests the precondition mechanism: txn.update is called ONLY when the
    snapshot inside the transaction shows ``due == expected_due``. This
    is the cron-side primitive that prevents two concurrent ticks from
    both reschedulin the same fire-time (defect #3 of the RFC).

    Concurrency note: real cross-process safety is provided by Firestore
    transaction OCC. These wire tests verify we USE the transaction
    correctly (read snapshot inside txn, compare due, conditionally
    write). End-to-end safety against the live emulator is exercised
    manually via ``make dev-emulator`` when modifying transaction code.
    """

    @pytest.fixture(autouse=True)
    def patch_async_transactional(self):
        """Make firestore.async_transactional a pass-through so the
        decorated function runs synchronously with our mock transaction."""
        with patch(
            "src.adapters.firestore_agent_note_adapter.firestore.async_transactional",
            side_effect=lambda fn: fn,
        ):
            yield

    def _setup(self, db_mock, col_mock, snapshot_data, *, exists=True):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, snapshot_data, exists=exists,
        ))
        col_mock.document.return_value = doc_ref

        transaction = MagicMock()
        transaction.update = MagicMock()
        db_mock.transaction.return_value = transaction
        return doc_ref, transaction

    async def test_returns_true_and_updates_when_due_matches(
        self, adapter, db_mock, col_mock,
    ):
        expected_due = _FUTURE
        next_due = _FUTURE + timedelta(days=1)
        last_fired = _NOW

        doc_ref, transaction = self._setup(
            db_mock, col_mock,
            _make_note_data(due=expected_due),
        )

        result = await adapter.reschedule_if_due_at(
            note_id=_NOTE_ID,
            expected_due=expected_due,
            next_due=next_due,
            last_fired=last_fired,
        )

        assert result is True
        transaction.update.assert_called_once_with(
            doc_ref, {"due": next_due, "last_fired": last_fired},
        )

    async def test_returns_false_when_due_already_moved(
        self, adapter, db_mock, col_mock,
    ):
        """Concurrent tick scenario: snapshot reads a `due` newer than
        what this caller expected → precondition fails → no write."""
        expected_due = _FUTURE
        snapshot_due = _FUTURE + timedelta(hours=1)  # already moved

        _, transaction = self._setup(
            db_mock, col_mock,
            _make_note_data(due=snapshot_due),
        )

        result = await adapter.reschedule_if_due_at(
            note_id=_NOTE_ID,
            expected_due=expected_due,
            next_due=_FUTURE + timedelta(days=1),
            last_fired=_NOW,
        )

        assert result is False
        transaction.update.assert_not_called()

    async def test_returns_false_when_doc_missing(
        self, adapter, db_mock, col_mock,
    ):
        _, transaction = self._setup(
            db_mock, col_mock, {}, exists=False,
        )

        result = await adapter.reschedule_if_due_at(
            note_id=_NOTE_ID,
            expected_due=_FUTURE,
            next_due=_FUTURE + timedelta(days=1),
            last_fired=_NOW,
        )

        assert result is False
        transaction.update.assert_not_called()

    async def test_returns_false_for_empty_note_id(
        self, adapter, db_mock, col_mock,
    ):
        result = await adapter.reschedule_if_due_at(
            note_id="",
            expected_due=_FUTURE,
            next_due=_FUTURE + timedelta(days=1),
            last_fired=_NOW,
        )

        assert result is False
        col_mock.document.assert_not_called()
        db_mock.transaction.assert_not_called()

    async def test_naive_datetime_in_storage_compared_via_utc_normalisation(
        self, adapter, db_mock, col_mock,
    ):
        """Firestore returns naive UTC datetimes; expected_due may be
        tz-aware. ``_ensure_utc`` normalises both sides — verify the
        match still succeeds."""
        # Storage holds naive (no tzinfo) — Firestore quirk.
        storage_due = _FUTURE.replace(tzinfo=None)
        # Caller passes tz-aware UTC.
        expected_due = _FUTURE  # tz-aware

        doc_ref, transaction = self._setup(
            db_mock, col_mock,
            _make_note_data(due=storage_due),
        )

        result = await adapter.reschedule_if_due_at(
            note_id=_NOTE_ID,
            expected_due=expected_due,
            next_due=_FUTURE + timedelta(days=1),
            last_fired=_NOW,
        )

        assert result is True
        transaction.update.assert_called_once()


# ---------------------------------------------------------------------------
# delete_if_due_at tests
# ---------------------------------------------------------------------------


class TestDeleteIfDueAt:

    @pytest.fixture(autouse=True)
    def patch_async_transactional(self):
        with patch(
            "src.adapters.firestore_agent_note_adapter.firestore.async_transactional",
            side_effect=lambda fn: fn,
        ):
            yield

    def _setup(self, db_mock, col_mock, snapshot_data, *, exists=True):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, snapshot_data, exists=exists,
        ))
        col_mock.document.return_value = doc_ref

        transaction = MagicMock()
        transaction.delete = MagicMock()
        db_mock.transaction.return_value = transaction
        return doc_ref, transaction

    async def test_returns_true_and_deletes_when_match_and_owned(
        self, adapter, db_mock, col_mock,
    ):
        doc_ref, transaction = self._setup(
            db_mock, col_mock, _make_note_data(due=_FUTURE),
        )

        result = await adapter.delete_if_due_at(
            note_id=_NOTE_ID, user_id=_USER_ID, expected_due=_FUTURE,
        )

        assert result is True
        transaction.delete.assert_called_once_with(doc_ref)

    async def test_returns_false_on_ownership_mismatch(
        self, adapter, db_mock, col_mock,
    ):
        _, transaction = self._setup(
            db_mock, col_mock,
            _make_note_data(user_id=_OTHER_USER_ID, due=_FUTURE),
        )

        result = await adapter.delete_if_due_at(
            note_id=_NOTE_ID, user_id=_USER_ID, expected_due=_FUTURE,
        )

        assert result is False
        transaction.delete.assert_not_called()

    async def test_returns_false_when_due_moved(
        self, adapter, db_mock, col_mock,
    ):
        _, transaction = self._setup(
            db_mock, col_mock,
            _make_note_data(due=_FUTURE + timedelta(hours=1)),
        )

        result = await adapter.delete_if_due_at(
            note_id=_NOTE_ID, user_id=_USER_ID, expected_due=_FUTURE,
        )

        assert result is False
        transaction.delete.assert_not_called()

    async def test_returns_false_when_doc_missing(
        self, adapter, db_mock, col_mock,
    ):
        _, transaction = self._setup(
            db_mock, col_mock, {}, exists=False,
        )

        result = await adapter.delete_if_due_at(
            note_id=_NOTE_ID, user_id=_USER_ID, expected_due=_FUTURE,
        )

        assert result is False
        transaction.delete.assert_not_called()

    async def test_returns_false_for_empty_note_id(self, adapter, db_mock, col_mock):
        result = await adapter.delete_if_due_at(
            note_id="", user_id=_USER_ID, expected_due=_FUTURE,
        )

        assert result is False
        col_mock.document.assert_not_called()


# ---------------------------------------------------------------------------
# mark_fire_delivered tests
# ---------------------------------------------------------------------------


class TestMarkFireDelivered:

    async def test_writes_last_delivered_due(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        await adapter.mark_fire_delivered(note_id=_NOTE_ID, due_at=_FUTURE)

        doc_ref.update.assert_called_once_with({"last_delivered_due": _FUTURE})

    async def test_empty_note_id_is_noop(self, adapter, col_mock):
        await adapter.mark_fire_delivered(note_id="", due_at=_FUTURE)
        col_mock.document.assert_not_called()


# ---------------------------------------------------------------------------
# _dict_to_note: last_delivered_due round-trip
# ---------------------------------------------------------------------------


class TestDictToNoteLastDeliveredDue:

    async def test_last_delivered_due_passes_through(self, adapter, col_mock):
        delivered_due = _NOW - timedelta(days=1)
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, {**_make_note_data(), "last_delivered_due": delivered_due},
        ))
        col_mock.document.return_value = doc_ref

        result = await adapter.get_note(user_id=_USER_ID, note_id=_NOTE_ID)

        assert result is not None
        assert result.last_delivered_due == delivered_due

    async def test_last_delivered_due_absent_yields_none(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data(),  # no last_delivered_due key
        ))
        col_mock.document.return_value = doc_ref

        result = await adapter.get_note(user_id=_USER_ID, note_id=_NOTE_ID)

        assert result is not None
        assert result.last_delivered_due is None

    async def test_naive_last_delivered_due_normalised_to_utc(self, adapter, col_mock):
        naive = (_NOW - timedelta(days=1)).replace(tzinfo=None)
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, {**_make_note_data(), "last_delivered_due": naive},
        ))
        col_mock.document.return_value = doc_ref

        result = await adapter.get_note(user_id=_USER_ID, note_id=_NOTE_ID)

        assert result is not None
        assert result.last_delivered_due is not None
        assert result.last_delivered_due.tzinfo is timezone.utc


# ---------------------------------------------------------------------------
# Misc legacy paths (cover deprecated + dict-to-note edge cases)
# ---------------------------------------------------------------------------


class TestLegacyAndEdgeCases:

    async def test_list_due_reminders_returns_all_due_docs(
        self, adapter, col_mock,
    ):
        """Cross-user query: filter due <= as_of, return all matching."""
        snap = _make_doc_snapshot(_NOTE_ID, _make_note_data(due=_PAST))
        query = MagicMock()
        query.get = AsyncMock(return_value=[snap])
        col_mock.where.return_value = query

        result = await adapter.list_due_reminders(as_of=_NOW)

        assert len(result) == 1
        assert result[0].note_id == _NOTE_ID

    async def test_dict_to_note_invalid_complexity_falls_back_to_none(
        self, adapter, col_mock,
    ):
        """Defensive: an unknown complexity string in storage must NOT raise.
        Logged at debug, complexity = None.
        """
        data = {**_make_note_data(), "complexity": "not_a_real_tier"}
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(_NOTE_ID, data))
        col_mock.document.return_value = doc_ref

        result = await adapter.get_note(user_id=_USER_ID, note_id=_NOTE_ID)

        assert result is not None
        assert result.complexity is None
