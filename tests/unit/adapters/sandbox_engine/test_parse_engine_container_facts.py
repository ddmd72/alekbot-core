"""`parse.container_facts` on the real drive child (EXTERNAL_FILES D13, owner decision O2): the engine runs one child on
the body, its status travels as the detail of an `ok` answer and the body is the validated result. Synthetic ZIPs built
in the test: a macro-bearing OOXML, a plain one, a member-count overflow, not a ZIP."""
import json
import os
import sys

import pytest

from src.adapters.sandbox_engine import parse_engine
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.parse_engine import ParseEngine
from src.domain.sandbox_contract import SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus
from src.utils.document_hardening import MAX_ZIP_MEMBERS
from tests.support import zip_containers as zc

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
                       reason="children run as the invoking user: needs a non-root user"),
]

LIMITS = SandboxLimits(wall_s=30.0, memory_bytes=1 << 30, cpu_s=30.0, file_bytes=64 * 1024 * 1024)


async def _run(tmp_path, body: bytes):
    workdir = tmp_path / "work"
    workdir.mkdir()
    out_root = tmp_path / "out"
    out_root.mkdir(mode=0o700)
    (workdir / "body").write_bytes(body)
    engine = ParseEngine(ChildLauncher(nproc=4000, nofile=256), op=SandboxOp.PARSE_CONTAINER_FACTS,
                         output_root=str(out_root), child_script=parse_engine._CHILD_SCRIPT)
    result = await engine.run(str(workdir / "body"), str(workdir), LIMITS,
                              SandboxMeta(request_id="r" * 16, limits=LIMITS))
    body_out = None
    if result.output_path:
        with open(result.output_path, "rb") as fh:
            body_out = json.loads(fh.read())
    request = json.loads((workdir / "request-0.json").read_text())
    return result, body_out, request


async def test_a_macro_bearing_docm(tmp_path):
    result, body, request = await _run(tmp_path, zc.macro_docm())
    assert (result.status, result.detail) == (SandboxStatus.OK, "zip")
    assert body == {"status": "zip", "members": 3, "vba_project": True, "macro_content_type": True,
                    "odf_macros": False}
    assert request["op"] == "container_facts" and request["path"].endswith("body")


async def test_a_plain_docx(tmp_path):
    result, body, _ = await _run(tmp_path, zc.plain_docx())
    assert (result.status, result.detail) == (SandboxStatus.OK, "zip")
    assert (body["vba_project"], body["macro_content_type"], body["odf_macros"]) == (False, False, False)


async def test_a_member_count_overflow(tmp_path):
    result, body, _ = await _run(tmp_path, zc.many_members(MAX_ZIP_MEMBERS + 1))
    assert (result.status, result.detail) == (SandboxStatus.OK, "too_large")
    assert body["members"] == MAX_ZIP_MEMBERS + 1 and "vba_project" not in body


async def test_not_a_zip(tmp_path):
    result, body, _ = await _run(tmp_path, b"%PDF-1.7\ninvoice FA-0137")
    assert (result.status, result.detail, body) == (SandboxStatus.OK, "not_zip", {"status": "not_zip"})
