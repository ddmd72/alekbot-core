"""The parse additions to the sandbox contract (UNTRUSTED_CONTENT_SANDBOX_RFC §4.7; plan P1): ops, the `memory` status,
the window plan of `parse.pdf_windows`, the caps sized from the PDF product cap, and the mirrored constants."""
import pytest

from src.adapters import subprocess_content_extractor as in_process
from src.domain import sandbox_contract as sc
from src.domain.drive_index_params import EXTRACT_WALL_TIMEOUT_S, MAX_PDF_DOWNLOAD_BYTES, MODEL_EXTRACT_MAX_PAGES
from src.domain.sandbox_contract import (
    SandboxContractError, SandboxLimits, SandboxMeta, SandboxOp, SandboxOutcome, SandboxStatus,
    decode_meta, decode_outcome, encode_meta, encode_outcome,
)

_LIMITS = SandboxLimits(wall_s=60.0, memory_bytes=1 << 30, cpu_s=30.0, file_bytes=1 << 20)
_ID = "r" * 16


def _header(**extra):
    import base64
    import json

    obj = {"request_id": _ID, "limits": {"wall_s": 1, "memory_bytes": 1, "cpu_s": 1, "file_bytes": 1}, **extra}
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def test_parse_ops_have_their_wire_names():
    assert {SandboxOp.PARSE_EXTRACT.value, SandboxOp.PARSE_PDF_PAGE_COUNT.value, SandboxOp.PARSE_PDF_WINDOWS.value} == {
        "parse.extract", "parse.pdf_page_count", "parse.pdf_windows"}
    assert "parse.pdf_window" not in {op.value for op in SandboxOp}   # one window is a batch of one


def test_memory_status_round_trips_in_an_outcome():
    back = decode_outcome(encode_outcome(SandboxOutcome(status=SandboxStatus.MEMORY, detail="killed")))
    assert back.status is SandboxStatus.MEMORY and back.detail == "killed"


def test_windows_round_trip():
    plan = ((0, 20), (20, 20), (40, 3))
    back = decode_meta(encode_meta(SandboxMeta(request_id=_ID, limits=_LIMITS, kind="pdf", windows=plan)))
    assert back.windows == plan and back.kind == "pdf"


def test_meta_without_windows_has_none():
    assert decode_meta(encode_meta(SandboxMeta(request_id=_ID, limits=_LIMITS))).windows is None


def test_the_maximum_plan_fits_the_meta_header():
    plan = tuple((i * 1000, 1000) for i in range(sc.SANDBOX_MAX_WINDOWS))
    assert decode_meta(encode_meta(SandboxMeta(request_id=_ID, limits=_LIMITS, windows=plan))).windows == plan


@pytest.mark.parametrize("bad", [
    [[True, 1]], [[0, True]], [[-1, 1]], [[0, 0]], [[0, -2]], [[0.5, 1]], [["0", 1]], [[0]], [[0, 1, 2]], [0, 1],
    [], None, "0,1", {"0": 1}, [[0, 1]] * (sc.SANDBOX_MAX_WINDOWS + 1),
])
def test_decode_refuses_a_malformed_window_plan(bad):
    with pytest.raises(SandboxContractError):
        decode_meta(_header(windows=bad))


@pytest.mark.parametrize("bad", [((True, 1),), ((-1, 1),), ((0, 0),), (), ((0, 1, 2),)])
def test_encode_refuses_a_malformed_window_plan(bad):
    with pytest.raises(SandboxContractError):
        encode_meta(SandboxMeta(request_id=_ID, limits=_LIMITS, windows=bad))


def test_the_window_bound_mirrors_the_models_page_budget():
    assert sc.SANDBOX_MAX_WINDOWS == MODEL_EXTRACT_MAX_PAGES == 200


def test_child_ceilings_equal_the_in_process_extract_limits():
    assert sc.CEILING_MEMORY_BYTES == in_process.EXTRACT_MEMORY_LIMIT_BYTES
    assert sc.CEILING_CPU_S == in_process.EXTRACT_CPU_LIMIT_S
    assert sc.CEILING_FILE_BYTES == in_process.EXTRACT_FILE_LIMIT_BYTES
    assert sc.CEILING_WALL_S == EXTRACT_WALL_TIMEOUT_S


def test_the_request_cap_is_the_pdf_product_cap_plus_a_margin_and_the_response_cap_matches():
    assert sc.PDF_PRODUCT_CAP_BYTES == MAX_PDF_DOWNLOAD_BYTES == 100 * 1024 * 1024
    assert sc.REQUEST_CAP_BYTES == MAX_PDF_DOWNLOAD_BYTES + sc.REQUEST_MARGIN_BYTES
    assert sc.REQUEST_CAP_BYTES > MAX_PDF_DOWNLOAD_BYTES
    assert sc.RESPONSE_CAP_BYTES == sc.REQUEST_CAP_BYTES
