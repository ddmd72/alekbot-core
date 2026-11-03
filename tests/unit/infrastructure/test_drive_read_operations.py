"""`DriveReadOperations`: the drive connector's read-only facade over `DriveOperations` (engineering call 6). Exactly
five reads, a subset of `READ_METHODS`; each returns the wrapped call's outcome with its arguments unchanged; `open`
always runs the media pre-check; nothing else of `DriveOperations` is reachable. Invented items only."""
from __future__ import annotations

import inspect
from typing import Any, Dict, List, Tuple

import pytest

from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_withheld import Withheld
from src.domain.user_drive import DriveItem
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_read_operations import DriveReadOperations
from tests.support.drive_service_connections import ConnectionReads, ref

FIVE = {"search", "find_passages", "read_passages", "list_folder", "open"}
CTX = {"user_id": "u1", "account_id": "a1"}
VIDEO = DriveItem(item_id="v1", name="standup.mp4", path="R&D/standup.mp4", is_folder=False, size_bytes=4000,
                  mime_type="video/mp4")


def _public(obj: Any) -> set:
    return {name for name in dir(obj) if not name.startswith("_")}


class Recorder:
    """Stands in for DriveOperations: every method records its call and returns its own outcome."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, tuple, Dict[str, Any]]] = []

    def __getattr__(self, name: str):
        async def method(*args: Any, **kwargs: Any) -> DriveOutcome:
            self.calls.append((name, args, kwargs))
            return DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text=f"from {name}", success=True)
        return method


class TestSurface:
    def test_exactly_the_five_reads(self):
        assert _public(DriveReadOperations) == FIVE
        assert _public(DriveReadOperations(Recorder())) == FIVE   # type: ignore[arg-type]

    def test_the_five_are_reads_of_drive_operations(self):
        assert FIVE <= DriveOperations.READ_METHODS
        assert FIVE.isdisjoint(DriveOperations.MUTATION_METHODS)

    @pytest.mark.parametrize("name", sorted((DriveOperations.READ_METHODS | DriveOperations.MUTATION_METHODS
                                             | {"configured", "keep_alive", "post_late_note_receipt"}) - FIVE))
    def test_nothing_else_is_reachable(self, name):
        facade = DriveReadOperations(Recorder())   # type: ignore[arg-type]
        assert not hasattr(facade, name)
        with pytest.raises(AttributeError):
            facade.extra = 1   # type: ignore[attr-defined]

    def test_open_takes_no_original_and_no_media_switch(self):
        params = inspect.signature(DriveReadOperations.open).parameters
        assert "original" not in params and "withhold_media" not in params


class TestDelegation:
    @pytest.mark.parametrize("name, args, kwargs, expected_kwargs", [
        ("search", (CTX, "u1"), dict(query="Tom's plan", folder="R&D", kind="docx", date_from="2026-01-01",
                                     date_to="2026-02-01", drives="all"),
         dict(query="Tom's plan", folder="R&D", kind="docx", date_from="2026-01-01", date_to="2026-02-01",
              drives="all")),
        ("find_passages", (CTX, "u1"), dict(question="Who signs FA-0137?", folder="R&D", drives="a1",
                                            file_refs=["drive:a1:d1"], shown=frozenset({("a1", ("d1", 1, 0))}),
                                            shown_folders=frozenset({"f1"}), described_files=frozenset({"d1"})),
         dict(question="Who signs FA-0137?", folder="R&D", drives="a1", file_refs=["drive:a1:d1"],
              shown=frozenset({("a1", ("d1", 1, 0))}), shown_folders=frozenset({"f1"}),
              described_files=frozenset({"d1"}))),
        ("read_passages", (CTX, "u1"), dict(file_ref="drive:a1:d1", passages="from 11",
                                            shown_folders=frozenset({"f1"}), described_files=frozenset({"d1"})),
         dict(file_ref="drive:a1:d1", passages="from 11", shown_folders=frozenset({"f1"}),
              described_files=frozenset({"d1"}))),
    ])
    async def test_each_read_returns_the_wrapped_outcome(self, name, args, kwargs, expected_kwargs):
        rec = Recorder()
        out = await getattr(DriveReadOperations(rec), name)(*args, **kwargs)   # type: ignore[arg-type]
        assert out.text == f"from {name}"
        assert rec.calls == [(name, args, expected_kwargs)]

    async def test_list_folder(self):
        rec = Recorder()
        out = await DriveReadOperations(rec).list_folder(CTX, "u1", "R&D")   # type: ignore[arg-type]
        assert out.text == "from list_folder" and rec.calls == [("list_folder", (CTX, "u1", "R&D"), {})]

    async def test_open_always_withholds_media_and_keeps_the_deadline(self):
        rec = Recorder()
        out = await DriveReadOperations(rec).open(CTX, "u1", "drive:a1:d1", deadline_at=1_090.0)   # type: ignore[arg-type]
        assert out.text == "from open"
        assert rec.calls == [("open", (CTX, "u1", "drive:a1:d1"), {"deadline_at": 1_090.0, "withhold_media": True})]


# -- open through the real DriveOperations ---------------------------------------------------------------------------

class Conversion:
    def __init__(self) -> None:
        self.downloads: List[str] = []

    async def resolve_bytes(self, ref: str, user_id: str) -> bytes:
        self.downloads.append(ref)
        return b"frames"


class Drive(ConnectionReads):
    async def get_item(self, handle: Any, item_id: str) -> DriveItem:
        return VIDEO


class Notification:
    def __init__(self) -> None:
        self.files: List[dict] = []

    async def notify_file_bytes(self, **kw: Any) -> bool:
        self.files.append(kw)
        return True


async def test_a_video_opened_through_the_facade_is_never_downloaded_or_posted():
    conversion, notification = Conversion(), Notification()
    ops = DriveOperations(conversion_service=conversion, notification=notification, drive_service=Drive())
    out = await DriveReadOperations(ops).open(CTX, "u1", ref("v1"))
    assert out.withheld is Withheld.RECORDING_UNTRANSCRIBED and out.text == ""   # a video with no transcript
    assert conversion.downloads == [] and notification.files == []


# -- open refuses every ref that is not a drive: ref (RFC §4.3, §12) --------------------------------------------------

@pytest.mark.parametrize("not_drive", [
    "invoice_FA-0137.pdf",                                    # a chat upload
    "chat:invoice_FA-0137.pdf",
    "workspace:binary_files/standup.mp4",
    "skill:notes/SKILL.md",
    "documents/u1/0f8fad5b-d9cb-469f-a165-70867728950e-Tom's plan.docx",   # a delivered document
    "",
])
async def test_open_refuses_a_ref_that_is_not_a_drive_ref_before_any_call(not_drive):
    rec = Recorder()
    out = await DriveReadOperations(rec).open(CTX, "u1", not_drive, deadline_at=1_090.0)   # type: ignore[arg-type]
    assert rec.calls == []
    assert (out.op, out.cls, out.success, out.text, out.items) == (DriveOp.OPEN, OutcomeClass.REFUSED, False, "", ())
    assert out.withheld is None   # the plain neutral refusal: no code tells "outside" from "missing"


async def test_a_drive_ref_still_delegates():
    rec = Recorder()
    await DriveReadOperations(rec).open(CTX, "u1", ref("d1"))   # type: ignore[arg-type]
    assert [c[0] for c in rec.calls] == ["open"] and rec.calls[0][1][2] == ref("d1")
