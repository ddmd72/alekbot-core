"""D13 on save (owner decision 2026-11-05): `DriveOperations.copy` of an `email:` source refuses an active kind before any
drive upload, with the same honest note as the send refusal; a plain attachment still saves; chat files stay unchecked.
The real conversion service over the email, storage and drive fakes; the assertions read the fake drive."""
import pytest

from src.domain import active_kind as ak
from src.domain.container_facts import ContainerFacts, ContainerStatus
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.email import EmailPart
from src.domain.email_open_texts import active_kind_refusal_note
from src.domain.email_ref import format_email_ref
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support import ole_containers as oc
from tests.support import zip_containers as zc
from tests.support.drive_access_stub import allow_access
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.unit.services.test_file_conversion_open import CountingStorage, RecordingDocuments
from tests.unit.services.test_file_conversion_open_email import FakeEmailSearch
from tests.unit.services.test_file_conversion_resolve_file import FakeContainers

MSG = "msg_invoice042"
CTX = {"account_id": "ann"}
DOCM = "application/vnd.ms-word.document.macroEnabled.12"
FILES = {
    "1": ("invoice_bathroom_es.pdf", "application/pdf", b"%PDF-invoice"),
    "2": ("invoice_bathroom_es.docm", DOCM, zc.macro_docm()),
    "3": ("invoice_bathroom_es.xls", "application/vnd.ms-excel", oc.excel_with_macros()),
    "4": ("notes.txt", "text/plain", oc.word_with_macros()),
    "5": ("invoice_bathroom_es.doc", "application/msword", oc.word_plain()),
    "6": ("invoice_bathroom_es.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", zc.plain_docx()),
}


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    repo = FakeDriveConnectionRepository()
    connections = connection_service(repo)
    w.conn = await seed(repo, user_id="ann", account_id="ann", connection_id="conn-1", tag="a1", kind="fake_a")
    w.drive = FakeDriveAdapter("fake_a")
    parts = [EmailPart(part_id=pid, filename=n, mime=m, size=len(d), handle=f"h{pid}")
             for pid, (n, m, d) in FILES.items()]
    w.mailbox = FakeEmailMailbox(messages={MSG: parts}, blobs={f"h{pid}": d for pid, (_, _, d) in FILES.items()})
    w.email = FakeEmailSearch(w.mailbox)
    w.containers = FakeContainers()
    conversion = FileConversionService(storage=CountingStorage({"invoice_bathroom_es.docm": zc.macro_docm()}), drive=w.drive,
                                       connections=connections, access=allow_access(), email=w.email)
    conversion.attach_documents(RecordingDocuments(), containers=w.containers)
    w.ops = DriveOperations(conversion_service=conversion, drive_service=UserDriveService(w.drive, connections))
    return w


def _stored(world):
    return {n.name: n.data for n in world.drive.tree(world.conn.handle).nodes.values() if not n.is_folder}


async def test_an_email_docm_is_refused_before_any_upload(world):
    world.containers.facts = ContainerFacts(status=ContainerStatus.ZIP, members=3, vba_project=True)
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, "2"), wait_s=5)
    assert (out.cls, out.success, out.text) == (OutcomeClass.REFUSED, False, active_kind_refusal_note(ak.OFFICE_MACROS))
    assert _stored(world) == {}


@pytest.mark.parametrize("pid", ["3", "4"])
async def test_an_email_ole_file_with_vba_is_refused_whatever_its_name(world, pid):
    world.containers.facts = ContainerFacts(status=ContainerStatus.OLE, members=4, vba_storage=True)
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, pid), wait_s=5)
    assert (out.cls, out.text) == (OutcomeClass.REFUSED, active_kind_refusal_note(ak.OFFICE_MACROS))
    assert _stored(world) == {}


async def test_an_email_ole_file_without_vba_is_saved(world):
    world.containers.facts = ContainerFacts(status=ContainerStatus.OLE, members=2)
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, "5"), wait_s=5)
    assert out.success and _stored(world) == {"invoice_bathroom_es.doc": FILES["5"][2]}


async def test_a_container_check_that_could_not_run_refuses_the_save(world):
    world.containers.raises = RuntimeError("boom")
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, "6"), wait_s=5)
    assert (out.cls, out.text) == (OutcomeClass.REFUSED, active_kind_refusal_note(ak.UNCHECKED))
    assert _stored(world) == {}


async def test_a_file_name_that_makes_the_file_active_is_refused(world):
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, "1"), file_name="invoice_bathroom_es.html", wait_s=5)
    assert (out.cls, out.text) == (OutcomeClass.REFUSED, active_kind_refusal_note(ak.HTML))
    assert _stored(world) == {}


async def test_a_plain_email_pdf_is_still_saved(world):
    out = await world.ops.copy(CTX, "ann", format_email_ref(MSG, "1"), wait_s=5)
    assert out.success and _stored(world) == {"invoice_bathroom_es.pdf": b"%PDF-invoice"}
    assert world.containers.calls == []


async def test_a_chat_file_named_docm_is_saved_unchecked(world):
    world.containers.raises = RuntimeError("D13 is email-only")
    out = await world.ops.copy(CTX, "ann", "invoice_bathroom_es.docm", wait_s=5)
    assert out.success and list(_stored(world)) == ["invoice_bathroom_es.docm"] and world.containers.calls == []
