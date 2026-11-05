"""`drive_parser.container_facts.zip_container_facts`: the reader the parser child runs for `parse.container_facts` (EXTERNAL_FILES
D13, owner decision O2), on synthetic ZIPs built in the test."""
import zipfile

import pytest

from src.adapters.drive_parser import container_facts as container_facts_module
from src.adapters.drive_parser.container_facts import zip_container_facts
from src.utils.document_hardening import MAX_ZIP_MEMBERS
from tests.support import zip_containers as zc


def _at(tmp_path, data: bytes, name: str = "attachment.bin") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_a_docm_shows_its_vba_part_and_its_content_type(tmp_path):
    assert zip_container_facts(_at(tmp_path, zc.macro_docm())) == {
        "status": "zip", "members": 3, "vba_project": True, "macro_content_type": True, "odf_macros": False}


def test_a_plain_docx_has_no_macro_fact(tmp_path):
    assert zip_container_facts(_at(tmp_path, zc.plain_docx())) == {
        "status": "zip", "members": 2, "vba_project": False, "macro_content_type": False, "odf_macros": False}


def test_a_macro_enabled_content_type_alone_is_a_fact(tmp_path):
    facts = zip_container_facts(_at(tmp_path, zc.macro_content_type()))
    assert (facts["vba_project"], facts["macro_content_type"]) == (False, True)


def test_an_odf_basic_library_is_a_fact(tmp_path):
    facts = zip_container_facts(_at(tmp_path, zc.odf_with_basic()))
    assert (facts["status"], facts["odf_macros"], facts["vba_project"]) == ("zip", True, False)


def test_more_members_than_the_cap_gives_no_facts(tmp_path):
    facts = zip_container_facts(_at(tmp_path, zc.many_members(MAX_ZIP_MEMBERS + 1)))
    assert facts["status"] == "too_large" and facts["members"] == MAX_ZIP_MEMBERS + 1
    assert "vba_project" not in facts


def test_a_content_types_part_above_its_cap_gives_no_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(container_facts_module, "MAX_CONTENT_TYPES_BYTES", 64)
    assert zip_container_facts(_at(tmp_path, zc.plain_docx()))["status"] == "too_large"


@pytest.mark.parametrize("data", [b"%PDF-1.7 invoice", b"", b"PK\x03\x04 but cut short"])
def test_not_a_zip(tmp_path, data):
    assert zip_container_facts(_at(tmp_path, data)) == {"status": "not_zip"}


def test_a_broken_member_is_an_error_not_a_crash(tmp_path):
    good = bytearray(zc.macro_docm())
    # corrupt the first local header's compressed data: the content-types member no longer inflates
    start = good.index(b"[Content_Types].xml") + len(b"[Content_Types].xml")
    good[start:start + 40] = b"\xff" * 40
    facts = zip_container_facts(_at(tmp_path, bytes(good)))
    assert facts["status"] == "error" and facts["detail"]


def test_nothing_but_the_content_types_member_is_inflated(tmp_path, monkeypatch):
    path = _at(tmp_path, zc.macro_docm())   # built before the patch: writing a ZIP opens members too
    opened = []
    real_open = zipfile.ZipFile.open

    def recording_open(self, member, *a, **k):
        opened.append(getattr(member, "filename", member))
        return real_open(self, member, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "open", recording_open)
    zip_container_facts(path)
    assert opened == ["[Content_Types].xml"]
