"""U5 frozen core (A-U5-2): the closed reader-family set, the unreported version, `reported_identity` over a child's
result, and the sandbox result schema admitting the two new keys (present, type-checked, or absent)."""
import json

import pytest

from src.domain.document_extraction import (
    PARSER, PARSER_VERSION, PARSER_VERSION_UNREPORTED, READER_FAMILIES, reported_identity,
)
from src.domain.sandbox_contract import SandboxOp
from src.domain.sandbox_parse_result import (
    RESULT_KEYS, ParseResultError, decode_parse_result, encode_parse_result,
)

EXTRACT = SandboxOp.PARSE_EXTRACT
BASE = {"status": "text", "text": "Invoice FA-0137 to Alex Doe", "pages_total": 1, "truncated": False, "detail": ""}


def _raw(obj) -> bytes:
    return json.dumps(obj).encode()


def test_the_unreported_version_is_3_and_the_current_one_is_not_below_it():
    assert PARSER_VERSION_UNREPORTED == 3 and PARSER_VERSION >= PARSER_VERSION_UNREPORTED


def test_the_family_set():
    assert READER_FAMILIES == {"pdf", "docx", "pptx", "epub", "epub_package", "msg", "xlsx", "xls", "odf", "html",
                               "text", "text_html", "csv", "subtitles", "rtf", "eml"}
    assert "" not in READER_FAMILIES


def test_a_reported_version_and_family_make_the_identity():
    assert reported_identity({**BASE, "parser_version": 4, "reader_family": "eml"}) == (f"{PARSER}@4", "eml")


def test_an_older_child_without_the_keys_is_parser_3_of_unknown_family():
    assert reported_identity(BASE) == (f"{PARSER}@3", "")


def test_an_unknown_family_is_unknown():
    assert reported_identity({**BASE, "parser_version": 4, "reader_family": "spreadsheet9"}) == (f"{PARSER}@4", "")


@pytest.mark.parametrize("bad", [{"parser_version": "4"}, {"parser_version": True}, {"parser_version": 0},
                                 {"parser_version": -1}, {"parser_version": 4.0}, {"reader_family": 7},
                                 {"reader_family": None}])
def test_a_bad_report_is_a_value_error(bad):
    with pytest.raises(ValueError):
        reported_identity({**BASE, **bad})


def test_the_result_schema_admits_both_keys():
    assert {"parser_version", "reader_family"} <= RESULT_KEYS
    obj = {**BASE, "parser_version": 4, "reader_family": "csv"}
    assert json.loads(encode_parse_result(decode_parse_result(_raw(obj), EXTRACT))) == obj


def test_the_result_schema_still_accepts_their_absence():
    assert decode_parse_result(_raw(BASE), EXTRACT) == BASE


@pytest.mark.parametrize("bad", [{"parser_version": "4"}, {"parser_version": -1}, {"parser_version": False},
                                 {"reader_family": 3}, {"reader_family": ["pdf"]}])
def test_the_result_schema_type_checks_them(bad):
    with pytest.raises(ParseResultError):
        decode_parse_result(_raw({**BASE, **bad}), EXTRACT)
