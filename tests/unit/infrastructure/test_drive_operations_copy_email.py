"""`DriveOperations.copy` with an `email:` source (EXTERNAL_FILES_RFC §4.2 "Resolver callers", "Naming on save"; U4b B1):
the bytes come from `resolve_file(action="save")`, the name and mime from the attachment, every note of the resolver is
a REFUSED line, and every other source kind keeps its path. The real conversion service over the email, storage and
drive fakes; the assertions read what ended up on the fake drive and the outcome's text."""
import pytest

from src.domain import drive_tool_texts as tt
from src.domain.document_limits import action_cap
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.email import EmailPart
from src.domain.email_open_texts import (
    ATTACHMENT_EMPTY_NOTE, ATTACHMENT_MISSING_NOTE, EMAIL_GONE_NOTE, EMAIL_NOT_CONNECTED_NOTE,
)
from src.domain.email_ref import format_email_ref
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_access_stub import allow_access
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.unit.services.test_file_conversion_open import CountingStorage
from tests.unit.services.test_file_conversion_open_email import FakeEmailSearch

PDF = "application/pdf"
MSG = "msg_invoice042"
CTX = {"account_id": "ann"}


def _part(part_id, name, size, handle, mime=PDF) -> EmailPart:
    return EmailPart(part_id=part_id, filename=name, mime=mime, size=size, handle=handle)


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    repo = FakeDriveConnectionRepository()
    connections = connection_service(repo)
    w.conn = await seed(repo, user_id="ann", account_id="ann", connection_id="conn-1", tag="a1", kind="fake_a")
    w.drive = FakeDriveAdapter("fake_a")
    w.mailbox = FakeEmailMailbox(
        messages={MSG: [_part("1.2", "invoice_bathroom_es.pdf", 2048, "h-bathroom"),
                        _part("1.3", "invoice_FA-0137.pdf", 1024, "h-inv"),
                        _part("2", "empty.txt", 0, "", mime="text/plain"),
                        _part("3", "huge_scan.pdf", action_cap() + 1, "h-huge")]},
        blobs={"h-bathroom": b"%PDF-bathroom", "h-inv": b"%PDF-invoice", "h-huge": b"%PDF"})
    w.email = FakeEmailSearch(w.mailbox)
    w.storage = CountingStorage({"chat_notes.txt": b"chat bytes"})
    conversion = FileConversionService(storage=w.storage, drive=w.drive, connections=connections,
                                       access=allow_access(), email=w.email)
    w.ops = DriveOperations(conversion_service=conversion, drive_service=UserDriveService(w.drive, connections))
    return w


def _stored(world):
    """name -> bytes of every file on the drive."""
    return {n.name: n.data for n in world.drive.tree(world.conn.handle).nodes.values() if not n.is_folder}


def _folder_of(world, name):
    tree = world.drive.tree(world.conn.handle)
    node = next(n for n in tree.nodes.values() if n.name == name)
    return tree.nodes[node.parent_id].name


async def test_an_email_ref_is_saved_under_the_part_s_name_with_the_part_s_bytes(world):
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, "1.2"), wait_s=5)
    assert out.success and out.op is DriveOp.COPY
    assert _stored(world) == {"invoice_bathroom_es.pdf": b"%PDF-bathroom"}
    assert _folder_of(world, "invoice_bathroom_es.pdf") == "Inbox"
    assert world.email.listings == 1 and [d[:2] for d in world.email.downloads] == [(MSG, "1.2")]


async def test_a_requested_folder_is_used(world):
    await world.ops.copy(CTX, "ann", format_email_ref(MSG, "1.3"), folder="Invoices", wait_s=5)
    assert _folder_of(world, "invoice_FA-0137.pdf") == "Invoices"


async def test_a_file_name_without_an_extension_keeps_the_part_s_extension(world):
    await world.ops.copy(CTX, "ann", format_email_ref(MSG, "1.2"), file_name="Invoice Reformas Exemplo", wait_s=5)
    assert _stored(world) == {"Invoice Reformas Exemplo.pdf": b"%PDF-bathroom"}


async def test_a_file_name_with_an_extension_is_used_as_given(world):
    await world.ops.copy(CTX, "ann", format_email_ref(MSG, "1.2"), file_name="invoice_painting_es.pdf", wait_s=5)
    assert _stored(world) == {"invoice_painting_es.pdf": b"%PDF-bathroom"}


@pytest.mark.parametrize("part, note", [("9", ATTACHMENT_MISSING_NOTE), ("2", ATTACHMENT_EMPTY_NOTE)])
async def test_a_part_note_is_a_refused_line_and_nothing_is_stored(world, part, note):
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, part), wait_s=5)
    assert (out.cls, out.text, out.success) == (OutcomeClass.REFUSED, note, False)
    assert _stored(world) == {}


async def test_a_gone_message_is_a_refused_line(world):
    out = await world.ops.copy(CTX, "ann", format_email_ref("msg_deleted", "1"), wait_s=5)
    assert (out.cls, out.text) == (OutcomeClass.REFUSED, EMAIL_GONE_NOTE)
    assert _stored(world) == {}


async def test_a_part_above_the_cap_is_refused_without_a_download(world):
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, "3"), wait_s=5)
    assert out.cls is OutcomeClass.REFUSED and not out.success
    assert world.email.downloads == [] and _stored(world) == {}


async def test_a_mailbox_not_connected_is_a_refused_line(world):
    world.email.connected = False
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, "1.2"), wait_s=5)
    assert (out.cls, out.text) == (OutcomeClass.REFUSED, EMAIL_NOT_CONNECTED_NOTE)
    assert _stored(world) == {}


async def test_a_chat_ref_still_goes_through_storage_and_is_named_from_the_ref(world):
    out = await world.ops.copy(CTX, "ann", "chat_notes.txt", wait_s=5)
    assert out.success
    assert _stored(world) == {"chat_notes.txt": b"chat bytes"}
    assert world.email.listings == 0


async def test_a_drive_source_is_refused_before_any_download(world):
    out = await world.ops.copy(CTX, "ann", "drive:a1:item-1", wait_s=5)
    assert (out.cls, out.success) == (OutcomeClass.REQUEST_ERROR, False)
    assert "move_file" in out.text and world.email.downloads == [] and _stored(world) == {}


def test_the_tool_text_names_an_email_ref_as_a_source():
    assert "email:" in tt.SOURCE_REF and "Never a drive: ref" in tt.SOURCE_REF
