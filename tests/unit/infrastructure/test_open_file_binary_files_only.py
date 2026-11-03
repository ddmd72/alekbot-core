"""open_file reads `workspace:` refs only under binary_files/ (AGENT_WORKSPACE_RFC §12)."""
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.native_format import NATIVE_FORMATS_KEY, NativeFormat, from_plain, to_plain
from src.infrastructure.drive_operations import DriveOperations
from tests.support.opened_file import opened_native

CTX = {"user_id": "u1", "account_id": "a1"}


class FakeConversion:
    def __init__(self) -> None:
        self.byte_reads: list[str] = []
        self.opens: list[tuple] = []

    async def resolve_bytes(self, ref, user_id):
        self.byte_reads.append(ref)
        return b"%PDF-1.4"

    async def open(self, ref, user_id, account_id, *, consumer, **kw):
        # the document service's NATIVE answer to a declared PDF (EXTERNAL_FILES_RFC P16)
        self.opens.append((ref, consumer))
        return opened_native("/tmp/lease.pdf", "application/pdf")


def _ops(conv):
    return DriveOperations(conversion_service=conv)


async def test_a_binary_files_ref_opens():
    conv = FakeConversion()
    # a consumer that reads PDFs natively: without a declaration the copy opens as text (EXTERNAL_FILES_RFC §4.3)
    native = to_plain([NativeFormat("application/pdf", 20 * 1024 * 1024, 100)])
    out = await _ops(conv).fetch_stored({**CTX, NATIVE_FORMATS_KEY: native}, "u1", "workspace:binary_files/lease.pdf")
    assert out.success and conv.opens == [("workspace:binary_files/lease.pdf", from_plain(native))]
    assert out.file_data == {"path": "/tmp/lease.pdf", "mime_type": "application/pdf"}


async def test_a_note_folder_ref_is_refused_with_the_workspace_tool_named():
    conv = FakeConversion()
    out = await _ops(conv).fetch_stored(CTX, "u1", "workspace:projects/garden-shed.md")
    assert out.cls is OutcomeClass.REFUSED and "view it with the workspace tool" in out.text
    assert conv.byte_reads == [] and conv.opens == []


async def test_the_old_folder_ref_is_refused_as_an_invalid_path():
    conv = FakeConversion()
    out = await _ops(conv).fetch_stored(CTX, "u1", "workspace:files/lease.pdf")
    assert out.cls is OutcomeClass.REFUSED and "invalid workspace path" in out.text
    assert conv.byte_reads == [] and conv.opens == []
