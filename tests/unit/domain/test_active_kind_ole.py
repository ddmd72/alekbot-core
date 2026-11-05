"""D13 for legacy OLE2 Office files (owner decision 2026-11-05): an OLE2 file with a VBA storage is `OFFICE_MACROS` under
any name; one without is not active; the check is needed from the OLE2 head and fails closed."""
import pytest

from src.domain import active_kind as ak
from src.domain.active_kind import active_kind, needs_container_check
from src.domain.container_facts import ContainerFacts, ContainerStatus, container_facts_from_result

OLE_HEAD = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
WITH_VBA = ContainerFacts(status=ContainerStatus.OLE, members=4, vba_storage=True)
NO_VBA = ContainerFacts(status=ContainerStatus.OLE, members=2)


@pytest.mark.parametrize("name, mime", [
    ("invoice_bathroom_es.doc", "application/msword"),
    ("invoice_bathroom_es.xls", "application/vnd.ms-excel"),
    ("invoice_bathroom_es.txt", "text/plain"),
    ("invoice_FA-0137.pdf", "application/pdf"),
])
def test_an_ole_file_with_a_vba_storage_is_active_under_any_name(name, mime):
    assert active_kind(name, mime, OLE_HEAD, WITH_VBA) == ak.OFFICE_MACROS


@pytest.mark.parametrize("name", ["invoice_bathroom_es.doc", "invoice_bathroom_es.xls", "invoice_bathroom_es.docx", "notes.txt"])
def test_an_ole_file_without_vba_is_not_active(name):
    assert active_kind(name, "application/octet-stream", OLE_HEAD, NO_VBA) is None


def test_an_ole_head_needs_the_container_check_whatever_the_name():
    assert needs_container_check("notes.txt", "text/plain", OLE_HEAD)


def test_the_ole_check_that_did_not_run_fails_closed():
    assert active_kind("invoice_bathroom_es.doc", "application/msword", OLE_HEAD, None) == ak.UNCHECKED
    erred = ContainerFacts(status=ContainerStatus.ERROR)
    assert active_kind("invoice_bathroom_es.doc", "application/msword", OLE_HEAD, erred) == ak.UNCHECKED


def test_a_doc_name_without_an_ole_head_is_not_checked_and_unchanged():
    assert not needs_container_check("invoice_bathroom_es.doc", "application/msword", b"{\\rtf1 invoice}")
    assert active_kind("invoice_bathroom_es.doc", "application/msword", b"{\\rtf1 invoice}", None) is None


def test_the_ole_facts_decode_from_the_op_result():
    facts = container_facts_from_result({"status": "ole", "members": 4, "vba_storage": True})
    assert facts == WITH_VBA and facts.checked and facts.carries_macros
    assert not container_facts_from_result({"status": "ole", "members": 2, "vba_storage": False}).carries_macros
