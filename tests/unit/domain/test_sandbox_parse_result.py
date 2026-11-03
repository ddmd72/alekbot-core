"""The parser child's result as the parse ops carry it: closed keys, per-op status sets, size bound."""
import json

import pytest

from src.adapters import subprocess_content_extractor as in_process
from src.domain.drive_index_params import MAX_TEXT_CHARS
from src.domain.sandbox_contract import SandboxOp
from src.domain.sandbox_parse_result import (
    DETAIL_MAX_CHARS, PARSE_RESULT_MAX_BYTES, RESULT_KEYS, ParseResultError, decode_parse_result, encode_parse_result,
)

EXTRACT, COUNT, WINDOWS = SandboxOp.PARSE_EXTRACT, SandboxOp.PARSE_PDF_PAGE_COUNT, SandboxOp.PARSE_PDF_WINDOWS


def _raw(obj) -> bytes:
    return json.dumps(obj).encode()


def test_key_set_is_closed_and_has_no_brief_and_no_identity():
    assert RESULT_KEYS == {"status", "text", "pages_total", "truncated", "detail", "script_rendered", "capture_signals",
                           "pages"}
    assert "brief" not in RESULT_KEYS


def test_round_trip_of_a_full_extract_result():
    obj = {"status": "text", "text": "Weekend in Coimbra", "pages_total": 2, "truncated": False, "detail": "",
           "script_rendered": True, "capture_signals": {"pages_sampled": 1}}
    assert json.loads(encode_parse_result(decode_parse_result(_raw(obj), EXTRACT))) == obj


def test_non_ascii_text_survives_as_utf8():
    body = encode_parse_result(decode_parse_result(_raw({"status": "text", "text": "Lúcia"}), EXTRACT))
    assert "Lúcia".encode() in body


@pytest.mark.parametrize("op, status", [
    (EXTRACT, s) for s in ("text", "no_text", "unsupported", "too_large", "encrypted", "error")
] + [(op, s) for op in (COUNT, WINDOWS) for s in ("encrypted", "unsupported", "timeout", "memory", "too_large", "error")]
 + [(COUNT, "text"), (WINDOWS, "text")])
def test_each_ops_statuses_are_accepted(op, status):
    obj = {"status": status, **({"pages_total": 1} if op is COUNT and status == "text" else {})}
    assert decode_parse_result(_raw(obj), op)["status"] == status


@pytest.mark.parametrize("op, status", [
    (EXTRACT, "timeout"), (EXTRACT, "memory"), (EXTRACT, "budget_exhausted"), (COUNT, "no_text"), (WINDOWS, "no_text"),
    (EXTRACT, ""), (EXTRACT, None),
])
def test_statuses_outside_the_ops_set_are_refused(op, status):
    with pytest.raises(ParseResultError):
        decode_parse_result(_raw({"status": status}), op)


def test_a_page_count_text_result_needs_pages_total():
    with pytest.raises(ParseResultError):
        decode_parse_result(_raw({"status": "text"}), COUNT)


@pytest.mark.parametrize("obj", [
    {"status": "text", "brief": "x"}, {"status": "text", "user_id": "u"}, {"text": "no status"}, [], "text",
    {"status": "text", "text": 42}, {"status": "text", "pages_total": True}, {"status": "text", "pages_total": -1},
    {"status": "text", "truncated": "no"}, {"status": "text", "script_rendered": 1}, {"status": "text", "detail": None},
    {"status": "text", "capture_signals": []}, {"status": "text", "pages": 1.5},
])
def test_malformed_results_are_refused(obj):
    with pytest.raises(ParseResultError):
        decode_parse_result(_raw(obj), EXTRACT)


def test_not_json_deep_json_and_a_non_parse_op_are_refused():
    with pytest.raises(ParseResultError):
        decode_parse_result(b"{not json", EXTRACT)
    with pytest.raises(ParseResultError):
        decode_parse_result(b"\xff\xfe", EXTRACT)
    with pytest.raises(ParseResultError):
        decode_parse_result(b"[" * 100000, EXTRACT)
    with pytest.raises(ParseResultError):
        decode_parse_result(_raw({"status": "text"}), SandboxOp.RENDER_DOCX)


def test_detail_is_cut_at_300():
    assert DETAIL_MAX_CHARS == 300
    assert len(decode_parse_result(_raw({"status": "error", "detail": "d" * 900}), EXTRACT)["detail"]) == 300


def test_the_size_bound_is_the_in_process_adapters_and_is_enforced():
    assert PARSE_RESULT_MAX_BYTES == MAX_TEXT_CHARS * 6 + 1024 * 1024 == in_process._RESULT_MAX_BYTES
    with pytest.raises(ParseResultError):
        decode_parse_result(b" " * (PARSE_RESULT_MAX_BYTES + 1), EXTRACT)


def test_encode_refuses_extra_keys_and_a_lone_surrogate():
    with pytest.raises(ParseResultError):
        encode_parse_result({"status": "text", "brief": "x"})
    with pytest.raises(ParseResultError):
        encode_parse_result(decode_parse_result(b'{"status": "text", "text": "\\ud800"}', EXTRACT))
