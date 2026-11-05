"""`document_hardening.container_facts` / `ole_container_facts`: the reader the parser child runs for
`parse.container_facts` on a legacy OLE2 file (EXTERNAL_FILES D13, owner decision 2026-11-05), on synthetic compound
files built in the test (`tests/support/ole_containers.py`)."""
import pytest

from src.utils.document_hardening import MAX_ZIP_MEMBERS, container_facts, ole_container_facts
from tests.support import ole_containers as oc
from tests.support import zip_containers as zc


def _at(tmp_path, data: bytes) -> str:
    path = tmp_path / "attachment.bin"
    path.write_bytes(data)
    return str(path)


@pytest.mark.parametrize("data", [oc.word_with_macros(), oc.excel_with_macros()])
def test_a_vba_storage_is_a_fact(tmp_path, data):
    assert container_facts(_at(tmp_path, data)) == {"status": "ole", "members": 4, "vba_storage": True}


def test_a_plain_word_file_has_no_vba_storage(tmp_path):
    assert container_facts(_at(tmp_path, oc.word_plain())) == {"status": "ole", "members": 2, "vba_storage": False}


def test_an_encrypted_ooxml_package_is_a_checked_file_without_macros(tmp_path):
    assert container_facts(_at(tmp_path, oc.encrypted_ooxml())) == {"status": "ole", "members": 2, "vba_storage": False}


def test_a_zip_keeps_the_zip_facts(tmp_path):
    facts = container_facts(_at(tmp_path, zc.macro_docm()))
    assert facts["status"] == "zip" and facts["vba_project"] is True and "vba_storage" not in facts


def test_a_file_that_is_neither_gives_not_zip(tmp_path):
    assert container_facts(_at(tmp_path, b"%PDF-1.7\ninvoice FA-0137")) == {"status": "not_zip"}


def test_more_entries_than_the_cap_gives_no_facts(tmp_path):
    facts = ole_container_facts(_at(tmp_path, oc.many_entries(MAX_ZIP_MEMBERS + 1)))
    assert facts["status"] == "too_large" and facts["members"] == MAX_ZIP_MEMBERS + 1


def test_a_broken_ole_file_is_an_error_not_a_pass(tmp_path):
    broken = oc.word_with_macros()[:512] + b"\xff" * 64   # the header says OLE2, the sectors are gone
    assert ole_container_facts(_at(tmp_path, broken))["status"] == "error"
