"""AgentNotePort kit (P11 · G1): the docstring of `src/ports/agent_note_port.py` checked against
`FirestoreAgentNoteAdapter` on the evaluating Firestore fake (H1), with the index refusal ON (both queries are
single-field). Assertions are on the raw stored documents and on what readers return.

`notes` is the one fixture that selects the backend. The clock is pinned and a note id is the creation millisecond
(open defect P11-G1-2), so every multi-note fixture is seeded as raw documents with distinct ids or re-pins the clock
before each create. Documents no writer produces (no `text`, a non-timestamp `due`) are not pinned: their behaviour
waits for the data audit.
"""
import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

import src.adapters.firestore_agent_note_adapter as note_module
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.domain.agent_note import NoteCreate, NoteUpdate
from src.domain.task_complexity import TaskComplexity
from src.ports.agent_note_port import AgentNotePort
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb, EvalDocRef
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
NOTES = "eval_orchestrator_notes"
T0 = UTC_MIDNIGHT.utc + timedelta(hours=8)      # 2026-10-27 08:00Z
DUE = T0 + timedelta(days=1)
DAD = dict(text="call dad", instruction="Remind Alex to call dad.")
GARDEN = dict(text="water the garden", instruction="Remind Kim to water the community garden.")
DENTIST = dict(text="dentist", instruction="Remind Alex of the appointment with Dr. Marta Quillen.")


class Notes:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={NOTES: "development_orchestrator_notes"})
        self.port: AgentNotePort = FirestoreAgentNoteAdapter(
            self.db, SimpleNamespace(orchestrator_notes_collection=NOTES))

    @property
    def col(self):
        return self.db.collection(NOTES)

    @property
    def docs(self) -> dict:
        return self.col.docs

    def seed(self, note_id: str, **fields) -> None:
        """A raw document, written as a legacy writer or another tool would have left it."""
        self.docs[note_id] = fields


@pytest.fixture
def notes(monkeypatch):
    pin_datetime(monkeypatch, note_module, T0)
    return Notes()


def at(monkeypatch, delta: timedelta) -> None:
    pin_datetime(monkeypatch, note_module, T0 + delta)


def io_trace(monkeypatch) -> list:
    """Every document read and write attempt, in order, as ("read" | "write", doc id): the race tests require from it
    that the interleaving they force actually happened (a serial run fails that require). Transaction commits are not
    writes through a reference, so a transactional race shows as the loser's extra read after its abort."""
    events: list = []

    def wrap(name, kind):
        real = getattr(EvalDocRef, name)

        async def traced(self, *args, **kwargs):
            events.append((kind, self.id))
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(EvalDocRef, name, traced)

    wrap("get", "read")
    for name in ("set", "create", "update", "delete"):
        wrap(name, "write")
    return events


async def create(notes: Notes, user_id="u1", due=DUE, texts=DAD, **extra):
    return await notes.port.create_note(NoteCreate(user_id=user_id, due=due, **texts, **extra))


def test_the_adapter_implements_the_port():
    assert issubclass(FirestoreAgentNoteAdapter, AgentNotePort)


# ----------------------------------------------------------------------------------------------------- create

CREATE_CASES = {
    "one_time": {},
    "rrule": {"recurrence": "FREQ=WEEKLY;BYDAY=TU"},
    "workspace_file": {"workspace_file": "projects/garden.md"},
    "complexity": {"complexity": TaskComplexity.DEEP_REASONING},
}


@pytest.mark.parametrize("case", list(CREATE_CASES))
async def test_create_stores_the_note_and_returns_it_with_its_id(notes, case):
    extra = CREATE_CASES[case]
    note = await create(notes, **extra)
    expected = {"user_id": "u1", "text": "call dad", "instruction": "Remind Alex to call dad.", "created_at": T0,
                "due": DUE, "last_fired": None, "recurrence": extra.get("recurrence")}
    if "workspace_file" in extra:
        expected["workspace_file"] = extra["workspace_file"]
    if "complexity" in extra:
        expected["complexity"] = extra["complexity"].value
    assert notes.docs == {note.note_id: expected}
    read = await notes.port.get_note("u1", note.note_id)
    assert read.complexity == extra.get("complexity")
    # the returned note's complexity is not populated (callers use note_id only); every other field matches the read
    assert dataclasses.replace(read, complexity=None) == dataclasses.replace(note, complexity=None)


async def test_create_refuses_more_than_25_words_and_writes_nothing(notes):
    with pytest.raises(ValueError):
        await create(notes, texts=dict(text=" ".join(["garden"] * 26), instruction="Remind Kim."))
    assert notes.docs == {}


async def test_create_refuses_the_31st_active_note_and_past_notes_do_not_count(notes, monkeypatch):
    for i in range(30):
        notes.seed(f"past{i}", user_id="u1", text="dentist", due=T0 - timedelta(days=1), created_at=T0)
    for i in range(29):
        notes.seed(f"active{i}", user_id="u1", text="call dad", due=DUE, created_at=T0)
    notes.seed("other", user_id="u2", text="water the garden", due=DUE, created_at=T0)
    thirtieth = await create(notes)
    assert thirtieth.note_id in notes.docs
    at(monkeypatch, timedelta(milliseconds=1))
    with pytest.raises(ValueError):
        await create(notes, texts=GARDEN)
    assert len(notes.docs) == 61


async def test_two_notes_created_a_millisecond_apart_are_both_kept(notes, monkeypatch):
    a = await create(notes, user_id="u1", texts=DAD)
    at(monkeypatch, timedelta(milliseconds=1))
    b = await create(notes, user_id="u2", texts=GARDEN)
    assert len(notes.docs) == 2
    assert (await notes.port.get_note("u1", a.note_id)).text == "call dad"
    assert (await notes.port.get_note("u2", b.note_id)).text == "water the garden"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G1-2: two notes created in one millisecond overwrite each other "
                          "(note id = creation millisecond, plain set)")
async def test_two_notes_created_in_the_same_millisecond_are_both_kept(notes):
    """Flips when the note id carries a uuid suffix (`f"{epoch_ms}-{uuid4().hex}"`), so ids never collide."""
    a = await create(notes, user_id="u1", texts=DAD)
    b = await create(notes, user_id="u2", texts=GARDEN)
    require(a.created_at == b.created_at == T0, "both notes were created at the same pinned instant")
    note = await notes.port.get_note("u1", a.note_id)
    assert len(notes.docs) == 2 and note is not None and note.text == "call dad"


# ------------------------------------------------------------------------------------------- get, delete

async def test_get_note_own_foreign_absent_empty(notes):
    note = await create(notes)
    assert await notes.port.get_note("u1", note.note_id) == note
    assert await notes.port.get_note("u2", note.note_id) is None
    assert await notes.port.get_note("u1", "1700000000000") is None
    assert await notes.port.get_note("u1", "") is None


async def test_delete_own_foreign_absent(notes):
    note = await create(notes)
    assert await notes.port.delete_note(note.note_id, "u2") is False
    assert note.note_id in notes.docs
    assert await notes.port.delete_note(note.note_id, "u1") is True
    assert notes.docs == {}
    assert await notes.port.delete_note(note.note_id, "u1") is False


async def test_delete_with_an_empty_id_raises(notes):
    with pytest.raises(ValueError):
        await notes.port.delete_note("", "u1")


# ----------------------------------------------------------------------------------------------------- update

async def test_update_patches_only_the_given_fields(notes):
    note = await create(notes, recurrence="FREQ=DAILY", workspace_file="projects/dad.md")
    before = dict(notes.docs[note.note_id])
    updated = await notes.port.update_note(NoteUpdate(note_id=note.note_id, user_id="u1", text="call dad tonight",
                                                      due=DUE + timedelta(hours=3),
                                                      complexity=TaskComplexity.SMALL_TALK))
    after = notes.docs[note.note_id]
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {"text", "due", "complexity"}
    assert after["text"] == "call dad tonight" and after["due"] == DUE + timedelta(hours=3)
    assert after["complexity"] == "small_talk"
    assert updated == await notes.port.get_note("u1", note.note_id)


@pytest.mark.parametrize("flag,field", [("clear_recurrence", "recurrence"), ("clear_workspace_file", "workspace_file")])
async def test_update_clear_flags_set_none(notes, flag, field):
    note = await create(notes, recurrence="FREQ=DAILY", workspace_file="projects/dad.md")
    updated = await notes.port.update_note(NoteUpdate(note_id=note.note_id, user_id="u1", **{flag: True}))
    assert notes.docs[note.note_id][field] is None
    assert getattr(updated, field) is None


async def test_update_sets_recurrence_instruction_and_workspace_file(notes):
    note = await create(notes)
    updated = await notes.port.update_note(NoteUpdate(
        note_id=note.note_id, user_id="u1", instruction="Remind Alex to call dad after dinner.",
        recurrence="FREQ=WEEKLY;BYDAY=SU", workspace_file="projects/dad.md"))
    stored = notes.docs[note.note_id]
    assert (stored["instruction"], stored["recurrence"], stored["workspace_file"]) == (
        "Remind Alex to call dad after dinner.", "FREQ=WEEKLY;BYDAY=SU", "projects/dad.md")
    assert updated == await notes.port.get_note("u1", note.note_id)


async def test_update_with_nothing_given_writes_nothing(notes):
    note = await create(notes)
    stamp = notes.col.versions[note.note_id]
    assert await notes.port.update_note(NoteUpdate(note_id=note.note_id, user_id="u1")) == note
    assert notes.col.versions[note.note_id] == stamp


@pytest.mark.parametrize("case", ["foreign", "absent", "empty", "overlong"])
async def test_update_refuses_foreign_absent_empty_overlong_and_writes_nothing(notes, case):
    note = await create(notes)
    before = dict(notes.docs[note.note_id])
    update = {
        "foreign": NoteUpdate(note_id=note.note_id, user_id="u2", text="water the garden"),
        "absent": NoteUpdate(note_id="1700000000000", user_id="u1", text="dentist"),
        "empty": NoteUpdate(note_id="", user_id="u1", text="dentist"),
        "overlong": NoteUpdate(note_id=note.note_id, user_id="u1", text=" ".join(["dad"] * 26)),
    }[case]
    with pytest.raises(ValueError):
        await notes.port.update_note(update)
    assert notes.docs == {note.note_id: before}


async def test_update_of_a_note_deleted_meanwhile_raises_not_found(notes, monkeypatch):
    note = await create(notes)
    notes.col.yield_on_get = True     # both read the note; the delete lands first, the update finds it gone
    events = io_trace(monkeypatch)
    deleted, updated = await asyncio.gather(
        notes.port.delete_note(note.note_id, "u1"),
        notes.port.update_note(NoteUpdate(note_id=note.note_id, user_id="u1", text="call dad tonight")),
        return_exceptions=True)
    require([kind for kind, _ in events][:2] == ["read", "read"], "both read the note before either wrote")
    assert deleted is True
    assert isinstance(updated, NotFound)
    assert notes.docs == {}


# ------------------------------------------------------------------------------------------------ list active

async def test_list_active_is_the_users_future_notes_oldest_first(notes):
    notes.seed("n1", user_id="u1", text="call dad", due=DUE, created_at=T0 + timedelta(minutes=2))
    notes.seed("n2", user_id="u1", text="dentist", due=DUE + timedelta(days=3), created_at=T0)
    notes.seed("n3", user_id="u1", text="water the garden", due=T0 - timedelta(hours=1), created_at=T0)
    notes.seed("n4", user_id="u1", text="call dad", due=T0, created_at=T0 - timedelta(days=1))
    notes.seed("n5", user_id="u2", text="water the garden", due=DUE, created_at=T0 - timedelta(days=2))
    active = await notes.port.list_active_notes("u1", as_of=T0)
    assert [n.note_id for n in active] == ["n2", "n1"]


async def test_list_active_as_of_epoch_returns_every_note_of_the_user(notes):
    notes.seed("n1", user_id="u1", text="call dad", due=DUE, created_at=T0)
    notes.seed("n2", user_id="u1", text="dentist", due=T0 - timedelta(hours=1), created_at=T0 - timedelta(days=1),
               last_fired=T0 - timedelta(hours=1))         # a claimed one-time note awaiting deletion
    notes.seed("n3", user_id="u2", text="water the garden", due=DUE, created_at=T0)
    every = await notes.port.list_active_notes("u1", as_of=datetime.fromtimestamp(0, tz=timezone.utc))
    assert [n.note_id for n in every] == ["n2", "n1"]


async def test_list_active_reads_legacy_shapes(notes):
    """The naive datetimes of `l4` exercise a defensive branch, not a contract: the client returns aware values."""
    naive = (DUE + timedelta(hours=1)).replace(tzinfo=None)
    notes.seed("l1", user_id="u1", text="call dad", due=DUE, created_at=T0)
    notes.seed("l2", user_id="u1", text="water the garden", instruction="Remind Kim to water the community garden.",
               due=DUE, created_at=T0 + timedelta(seconds=1), recurrence={"type": "daily", "interval": 2})
    notes.seed("l3", user_id="u1", text="dentist", due=DUE, created_at=T0 + timedelta(seconds=2),
               recurrence={"type": "weekly"}, complexity="bogus")
    notes.seed("l4", user_id="u1", text="call dad", due=naive, created_at=(T0 + timedelta(seconds=3)).replace(tzinfo=None),
               recurrence={"x": 1}, last_fired=T0.replace(tzinfo=None))
    by_id = {n.note_id: n for n in await notes.port.list_active_notes("u1", as_of=T0)}
    assert by_id["l1"].instruction == "call dad"                         # no instruction: reads the text
    assert by_id["l1"].workspace_file is None and by_id["l1"].recurrence is None
    assert by_id["l2"].recurrence == "FREQ=DAILY;INTERVAL=2"
    assert by_id["l3"].recurrence == "FREQ=WEEKLY" and by_id["l3"].complexity is None
    assert by_id["l4"].recurrence is None                                # unreadable: one-time
    assert by_id["l4"].due == DUE + timedelta(hours=1) and by_id["l4"].due.tzinfo is not None
    assert by_id["l4"].created_at.tzinfo is not None and by_id["l4"].last_fired == T0


# --------------------------------------------------------------------------------------------------- list due

async def test_list_due_is_cross_user_and_inclusive(notes):
    notes.seed("d1", user_id="u1", text="call dad", due=T0, created_at=T0 - timedelta(days=1))
    notes.seed("d2", user_id="u2", text="water the garden", due=T0 - timedelta(minutes=5), created_at=T0)
    notes.seed("d3", user_id="u1", text="dentist", due=DUE, created_at=T0)
    due = await notes.port.list_due_reminders(T0)
    assert {n.note_id for n in due} == {"d1", "d2"}
    assert {n.user_id for n in due} == {"u1", "u2"}


async def test_list_due_keeps_a_claimed_one_time_note_until_it_is_deleted(notes):
    notes.seed("d1", user_id="u1", text="dentist", due=T0 - timedelta(minutes=1), created_at=T0 - timedelta(days=1))
    assert await notes.port.claim_one_time_if_due_at("d1", "u1", T0 - timedelta(minutes=1), T0) is True
    assert [n.note_id for n in await notes.port.list_due_reminders(T0)] == ["d1"]
    assert await notes.port.delete_note("d1", "u1") is True
    assert await notes.port.list_due_reminders(T0) == []


# ----------------------------------------------------------------------------------------------- reschedule

async def test_reschedule_moves_due_and_stamps_last_fired(notes):
    note = await create(notes, due=T0, recurrence="FREQ=DAILY")
    assert await notes.port.reschedule_if_due_at(note.note_id, T0, T0 + timedelta(days=1), T0) is True
    stored = notes.docs[note.note_id]
    assert (stored["due"], stored["last_fired"]) == (T0 + timedelta(days=1), T0)


@pytest.mark.parametrize("case", ["moved", "absent", "empty"])
async def test_reschedule_refuses_a_moved_due_an_absent_note_and_an_empty_id(notes, case):
    note = await create(notes, due=T0, recurrence="FREQ=DAILY")
    before = dict(notes.docs[note.note_id])
    note_id, expected = {"moved": (note.note_id, T0 - timedelta(days=1)), "absent": ("1700000000000", T0),
                         "empty": ("", T0)}[case]
    assert await notes.port.reschedule_if_due_at(note_id, expected, T0 + timedelta(days=1), T0) is False
    assert notes.docs == {note.note_id: before}


async def test_reschedule_compares_a_naive_stored_due_as_utc_defensive_branch(notes):
    """A defensive branch, not a contract: the client always returns an aware Timestamp."""
    notes.seed("r1", user_id="u1", text="call dad", due=T0.replace(tzinfo=None), created_at=T0, recurrence="FREQ=DAILY")
    assert await notes.port.reschedule_if_due_at("r1", T0, T0 + timedelta(days=1), T0) is True
    assert notes.docs["r1"]["due"] == T0 + timedelta(days=1)


async def test_two_concurrent_reschedules_exactly_one_wins(notes, monkeypatch):
    note = await create(notes, due=T0, recurrence="FREQ=DAILY")
    notes.col.yield_on_get = True     # both transactions read the same due; the second commit aborts and re-reads
    events = io_trace(monkeypatch)
    results = await asyncio.gather(
        notes.port.reschedule_if_due_at(note.note_id, T0, T0 + timedelta(days=1), T0),
        notes.port.reschedule_if_due_at(note.note_id, T0, T0 + timedelta(days=2), T0 + timedelta(seconds=1)))
    require(events.count(("read", note.note_id)) >= 3, "both read the due before a commit, the loser re-read after its abort")
    assert sorted(results) == [False, True]
    winner = results.index(True)
    stored = notes.docs[note.note_id]
    assert stored["due"] == T0 + timedelta(days=1 + winner)
    assert stored["last_fired"] == T0 + timedelta(seconds=winner)


async def test_reschedule_has_no_ownership_check(notes):
    note = await create(notes, user_id="u2", due=T0, recurrence="FREQ=DAILY")
    assert await notes.port.reschedule_if_due_at(note.note_id, T0, T0 + timedelta(days=1), T0) is True
    assert notes.docs[note.note_id]["user_id"] == "u2"


# -------------------------------------------------------------------------------------------- one-time claim

async def test_claim_one_time_stamps_and_a_second_claim_is_refused(notes):
    note = await create(notes, due=T0)
    assert await notes.port.claim_one_time_if_due_at(note.note_id, "u1", T0, T0) is True
    assert await notes.port.claim_one_time_if_due_at(note.note_id, "u1", T0, T0 + timedelta(seconds=1)) is False
    stored = notes.docs[note.note_id]
    assert (stored["due"], stored["last_fired"]) == (T0, T0)


@pytest.mark.parametrize("case", ["foreign", "moved", "absent", "empty"])
async def test_claim_one_time_refuses_foreign_moved_absent(notes, case):
    note = await create(notes, due=T0)
    before = dict(notes.docs[note.note_id])
    note_id, user_id, expected = {"foreign": (note.note_id, "u2", T0), "moved": (note.note_id, "u1", DUE),
                                  "absent": ("1700000000000", "u1", T0), "empty": ("", "u1", T0)}[case]
    assert await notes.port.claim_one_time_if_due_at(note_id, user_id, expected, T0) is False
    assert notes.docs == {note.note_id: before}


async def test_two_concurrent_one_time_claims_exactly_one_wins(notes, monkeypatch):
    note = await create(notes, due=T0)
    notes.col.yield_on_get = True     # both transactions read the unclaimed note; the second commit aborts and re-reads
    events = io_trace(monkeypatch)
    results = await asyncio.gather(notes.port.claim_one_time_if_due_at(note.note_id, "u1", T0, T0),
                                   notes.port.claim_one_time_if_due_at(note.note_id, "u1", T0, T0))
    require(events.count(("read", note.note_id)) >= 3, "both read the note before a commit, the loser re-read after its abort")
    assert sorted(results) == [False, True]
    assert notes.docs[note.note_id]["last_fired"] == T0


# --------------------------------------------------------------------------------- delete_if_due_at (no caller)

@pytest.mark.parametrize("case,deleted", [("own", True), ("foreign", False), ("moved", False), ("absent", False),
                                          ("empty", False)])
async def test_delete_if_due_at_own_matching_foreign_moved(notes, case, deleted):
    note = await create(notes, due=T0)
    note_id, user_id, expected = {"own": (note.note_id, "u1", T0), "foreign": (note.note_id, "u2", T0),
                                  "moved": (note.note_id, "u1", DUE), "absent": ("1700000000000", "u1", T0),
                                  "empty": ("", "u1", T0)}[case]
    assert await notes.port.delete_if_due_at(note_id, user_id, expected) is deleted
    assert (note.note_id in notes.docs) is (not deleted)


# --------------------------------------------------------------------------------------- mark_fire_delivered

async def test_mark_fire_delivered_stamps_and_a_repeat_is_identical(notes):
    note = await create(notes, due=T0)
    await notes.port.mark_fire_delivered(note.note_id, T0)
    first = dict(notes.docs[note.note_id])
    await notes.port.mark_fire_delivered(note.note_id, T0)
    assert notes.docs[note.note_id] == first
    assert (await notes.port.get_note("u1", note.note_id)).last_delivered_due == T0


async def test_mark_fire_delivered_on_a_deleted_note_raises_not_found(notes):
    note = await create(notes, due=T0)
    await notes.port.delete_note(note.note_id, "u1")
    with pytest.raises(NotFound):
        await notes.port.mark_fire_delivered(note.note_id, T0)
    assert notes.docs == {}


async def test_mark_fire_delivered_with_an_empty_id_writes_nothing(notes):
    clock = notes.db._clock
    await notes.port.mark_fire_delivered("", T0)
    assert notes.docs == {} and notes.col.versions == {} and notes.db._clock == clock


# ---------------------------------------------------------------------------------------------------- errors

async def test_a_backend_error_in_create_propagates(notes, monkeypatch):
    async def unavailable(self, data, merge=False):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(EvalDocRef, "set", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        await create(notes)
    assert notes.docs == {}


async def test_a_backend_error_in_list_due_propagates(notes):
    notes.seed("d1", user_id="u1", text="call dad", due=T0, created_at=T0)
    notes.col.fail = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        await notes.port.list_due_reminders(T0)
