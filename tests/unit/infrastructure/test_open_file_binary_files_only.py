"""open_file reads `workspace:` refs only under binary_files/ (AGENT_WORKSPACE_RFC §12)."""
from src.domain.drive_outcome_class import OutcomeClass
from src.infrastructure.drive_operations import DriveOperations

CTX = {"user_id": "u1", "account_id": "a1"}


class FakeConversion:
    def __init__(self) -> None:
        self.byte_reads: list[str] = []

    async def resolve_bytes(self, ref, user_id):
        self.byte_reads.append(ref)
        return b"%PDF-1.4"


def _ops(conv):
    return DriveOperations(conversion_service=conv)


async def test_a_binary_files_ref_opens():
    conv = FakeConversion()
    out = await _ops(conv).fetch_stored(CTX, "u1", "workspace:binary_files/lease.pdf")
    assert out.success and conv.byte_reads == ["workspace:binary_files/lease.pdf"]


async def test_a_note_folder_ref_is_refused_with_the_workspace_tool_named():
    conv = FakeConversion()
    out = await _ops(conv).fetch_stored(CTX, "u1", "workspace:projects/garden-shed.md")
    assert out.cls is OutcomeClass.REFUSED and "view it with the workspace tool" in out.text
    assert conv.byte_reads == []


async def test_the_old_folder_ref_is_refused_as_an_invalid_path():
    conv = FakeConversion()
    out = await _ops(conv).fetch_stored(CTX, "u1", "workspace:files/lease.pdf")
    assert out.cls is OutcomeClass.REFUSED and "invalid workspace path" in out.text
    assert conv.byte_reads == []
