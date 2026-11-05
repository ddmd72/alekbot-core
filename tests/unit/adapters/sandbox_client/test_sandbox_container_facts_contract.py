"""Wire contract of `parse.container_facts` (EXTERNAL_FILES D13, owner decision O2): the real sandbox app with the real
`ParseEngine` over the real drive child, behind `SandboxContentExtractor` over ASGI, against the reader the child runs
(`zip_container_facts`) in this process on the same bytes: equal facts for a macro OOXML, a plain one, an ODF with a
Basic library, a member-count overflow and a file that is not a ZIP (ADAPTER_WIRE_TESTING.md)."""
import os
import sys

import httpx
import pytest
from quart import Quart

from src.adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.parse_engine import ParseEngine
from src.domain.container_facts import ContainerStatus, container_facts_from_result
from src.domain.sandbox_contract import SandboxOp
from src.adapters.drive_parser.container_facts import zip_container_facts
from src.utils.document_hardening import MAX_ZIP_MEMBERS
from src.web import sandbox_app
from tests.support import zip_containers as zc
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, good_token
from tests.unit.web.test_sandbox_app import CEILING, FakeGate, FakeRecycler

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"), reason="needs a non-root user"),
]


def _over_the_real_app(tmp_path) -> SandboxContentExtractor:
    out_root = tmp_path / "out"
    out_root.mkdir(mode=0o700)
    work_root = tmp_path / "work"
    work_root.mkdir()
    op = SandboxOp.PARSE_CONTAINER_FACTS
    engines = {op: ParseEngine(ChildLauncher(nproc=4000, nofile=256), op=op, output_root=str(out_root))}
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        engines, FakeGate(), FakeRecycler(), CEILING,
        {"work_root": str(work_root), "engine_user": None, "recycle": "never", "output_root": str(out_root)}))
    return SandboxContentExtractor(SandboxHttpClient(BASE, good_token, transport=httpx.ASGITransport(app=app)))


async def test_the_sandboxed_facts_equal_the_reader_s_in_process(tmp_path):
    sandboxed = _over_the_real_app(tmp_path)
    cases = {"docm": zc.macro_docm(), "docx": zc.plain_docx(), "odt": zc.odf_with_basic(),
             "overflow": zc.many_members(MAX_ZIP_MEMBERS + 1), "pdf": b"%PDF-1.7\ninvoice FA-0137"}
    seen = {}
    for label, data in cases.items():
        local_path = tmp_path / f"{label}.bin"
        local_path.write_bytes(data)
        remote = await sandboxed.container_facts(data)
        assert remote == container_facts_from_result(zip_container_facts(str(local_path))), label
        seen[label] = remote
    assert seen["docm"].carries_macros and seen["odt"].carries_macros
    assert seen["docx"].checked and not seen["docx"].carries_macros
    assert seen["overflow"].status is ContainerStatus.TOO_LARGE and not seen["overflow"].checked
    assert seen["pdf"].status is ContainerStatus.NOT_ZIP
