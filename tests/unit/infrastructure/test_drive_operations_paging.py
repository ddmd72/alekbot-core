"""`DriveOperations.read_passages` with `from N` over the real search service (USER_DRIVE_INDEXING_RFC §29.5)."""
from __future__ import annotations

from unittest.mock import AsyncMock

from src.domain import drive_excerpt_render as R
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain import drive_ref
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from unittest.mock import MagicMock

from tests.unit.services.test_drive_search_excerpts import PassageWorld

CTX = {"user_id": "u", "account_id": "a1"}


def _setup(count=25):
    w = PassageWorld()
    item = w.doc("Oak Street parking rules.md", [f"Clause {n}. The gate stays closed." for n in range(1, count + 1)],
                 [0.1] * count, kind="md", content_version="c1", indexed_version="c1")
    drive = AsyncMock(spec=UserDriveService)
    ops = DriveOperations(conversion_service=MagicMock(spec=FileConversionService), drive_service=drive,
                          drive_search=w.service)
    return w, ops, drive_ref.format("a1", item)   # the harness connection's tag


async def test_from_one_is_a_page():
    w, ops, ref = _setup()
    out = await ops.read_passages(CTX, "u", file_ref=ref, passages="from 1")
    assert (out.op, out.cls, out.success) == (DriveOp.PASSAGES, OutcomeClass.OK, True)
    assert out.facts.paged and out.facts.excerpts
    assert "Clause 10." in out.text and "Clause 11." not in out.text
    assert out.text.splitlines()[-1].endswith("when the commission asks for it: from 11.")


async def test_past_the_end_is_ok_and_paged_not_not_found():
    w, ops, ref = _setup()
    reads = w.repo.calls["texts"]
    out = await ops.read_passages(CTX, "u", file_ref=ref, passages="from 26")
    assert out.cls is OutcomeClass.OK and out.success and out.facts.paged and not out.facts.excerpts
    assert "passage 26 is past the end" in out.text and R.NO_PASSAGES not in out.text
    assert "The last page starts at passage 16." in out.text
    assert w.repo.calls["texts"] == reads


async def test_a_plain_range_is_not_paged():
    w, ops, ref = _setup()
    out = await ops.read_passages(CTX, "u", file_ref=ref, passages="6-8")
    assert out.cls is OutcomeClass.OK and not out.facts.paged and out.facts.excerpts


async def test_a_bad_page_is_a_request_error():
    w, ops, ref = _setup()
    out = await ops.read_passages(CTX, "u", file_ref=ref, passages="from 0")
    assert out.cls is OutcomeClass.REQUEST_ERROR and not out.success
