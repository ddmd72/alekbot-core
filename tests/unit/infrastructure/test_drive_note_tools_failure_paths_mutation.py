"""`read_note_for_connector` failure paths (mutation survivors): each guarded call that raises answers its handled
outcome (a refusal with no text, or the unexpected-failure line), never a None."""
import pytest

from src.domain import drive_note_tool_texts as nt
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.user_drive import DriveItemNotFoundError
from src.infrastructure.drive_note_tools import read_note_for_connector

REF = "drive:a1:item-1"


class Target:
    handle = "h"
    item_id = "item-1"


class Items:
    def __init__(self, resolve=None, get=None):
        self.resolve, self.get = resolve, get

    async def resolve_ref(self, user_id, ref):
        if self.resolve:
            raise self.resolve
        return Target()

    async def get_item(self, handle, item_id):
        if self.get:
            raise self.get


class Service:
    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    async def get(self, user_id, ref, **kw):
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize("where", ["resolve", "get"])
async def test_a_refused_item_is_a_refusal_with_no_text_and_no_note_read(where):
    svc = Service(RuntimeError("must not be read"))
    items = Items(**{where: DriveItemNotFoundError("gone")})
    out = await read_note_for_connector(svc, items, "usr-alpha-1", REF, "")
    assert out is not None
    assert (out.op, out.cls, out.success, out.text) == (DriveOp.NOTE_READ, OutcomeClass.REFUSED, False, "")
    assert svc.calls == 0


async def test_an_unexpected_item_failure_is_the_unexpected_line_and_reads_no_note():
    svc = Service(RuntimeError("x"))
    out = await read_note_for_connector(svc, Items(resolve=RuntimeError("provider down")), "usr-alpha-1", REF, "")
    assert (out.cls, out.success, out.text) == (OutcomeClass.FAILED, False, nt.UNEXPECTED)
    assert svc.calls == 0


@pytest.mark.parametrize("ref,drive", [(REF, ""), ("", "Alpha")])
async def test_a_note_service_failure_is_the_unexpected_line(ref, drive):
    svc = Service(RuntimeError("store broke"))
    out = await read_note_for_connector(svc, Items(), "usr-alpha-1", ref, drive)
    assert (out.cls, out.success, out.text) == (OutcomeClass.FAILED, False, nt.UNEXPECTED)


async def test_a_store_outage_is_the_store_down_line():
    out = await read_note_for_connector(Service(DriveIndexStoreUnavailable()), Items(), "usr-alpha-1", REF, "")
    assert (out.cls, out.text) == (OutcomeClass.FAILED, nt.STORE_DOWN)
