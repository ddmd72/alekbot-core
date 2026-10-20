"""NodeDocxRunner vs SandboxDocxRunner: equal observable outcomes (plan G20; RFC §4.9, §4.10).

Real variant: client -> real sandbox_app (ASGI) -> real DocxRenderEngine -> real Node. The stub-engine variant runs
everywhere and pins the status -> DocxRunnerError mapping through the same real app and client.
"""
import os
import shutil
import sys

import httpx
import pytest

from src.adapters.node_docx_runner import NodeDocxRunner
from src.adapters.sandbox_client.sandbox_docx_runner import SandboxDocxRunner
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.docx_render_engine import DocxRenderEngine
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxLimits, SandboxStatus
from src.ports.docx_runner_port import DocxRunnerError
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, good_token, real_app_transport
from tests.unit.web.test_sandbox_app import FakeEngine

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_HAS_NODE = shutil.which("node") is not None and os.path.isdir(os.path.join(_ROOT, "docx_generator", "node_modules", "docx"))
needs_node = pytest.mark.skipif(
    not _HAS_NODE or os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
    reason="needs node + docx_generator/node_modules and a non-root user")

BIG = SandboxLimits(wall_s=120.0, memory_bytes=4 << 30, cpu_s=90.0, file_bytes=64 * 1024 * 1024)

_OK = """
const docx = require('docx');
let input = '';
process.stdin.on('data', d => input += d);
process.stdin.on('end', async () => {
  const spec = JSON.parse(input);
  const doc = new docx.Document({sections: [{children: [new docx.Paragraph(spec.title)]}]});
  process.stdout.write(await docx.Packer.toBuffer(doc));
});
"""
_THROW = "throw new Error('contract-boom-42');"
_HANG = "setInterval(() => {}, 1000);"
_SILENT = "process.stdin.resume(); process.stdin.on('end', () => {});"
_SPEC = '{"title": "Hello"}'


async def _no_sleep(_s):
    return None


@pytest.fixture(params=["node", "sandbox"])
def runner(request, tmp_path):
    if request.param == "node":
        return NodeDocxRunner()
    engine = DocxRenderEngine(ChildLauncher(user=None), output_root=_private(tmp_path))
    transport = real_app_transport(tmp_path, engine=engine, ceiling=BIG)
    client = SandboxHttpClient(BASE, good_token, transport=transport, sleep=_no_sleep)
    return SandboxDocxRunner(client, memory_bytes=BIG.memory_bytes)


def _private(tmp_path):
    root = tmp_path / "engine-out"
    root.mkdir(mode=0o700, exist_ok=True)
    return str(root)


async def _failure(runner, script, timeout=60) -> str:
    with pytest.raises(DocxRunnerError) as info:
        await runner.run(script, _SPEC, timeout)
    return str(info.value)


@needs_node
async def test_success_returns_docx_bytes(runner):
    assert (await runner.run(_OK, _SPEC, 60)).startswith(b"PK")


@needs_node
async def test_script_throwing_yields_runner_error_with_the_script_message(runner):
    text = await _failure(runner, _THROW)
    assert "contract-boom-42" in text and text.startswith("exit code 1")


@needs_node
async def test_hanging_script_hits_the_timeout(runner):
    assert await _failure(runner, _HANG, timeout=2) == "Node.js process timed out after 2s"


@needs_node
async def test_script_writing_nothing_gives_the_empty_output_error(runner):
    text = await _failure(runner, "process.stdout.write('');")
    assert text == "Node.js exited 0 but stdout is empty — no DOCX bytes produced"


# --- always-on: stub engine behind the real app and client -------------------------------------------------

@pytest.mark.parametrize("status,detail,expected", [
    (SandboxStatus.SCRIPT_ERROR, "exit code 1\nboom", "exit code 1\nboom"),
    (SandboxStatus.TIMEOUT, "Node.js process timed out after 2s", "Node.js process timed out after 2s"),
    (SandboxStatus.EMPTY_OUTPUT, "Node.js exited 0 but stdout is empty — no DOCX bytes produced",
     "Node.js exited 0 but stdout is empty — no DOCX bytes produced"),
])
async def test_stub_engine_failures_reach_the_model_verbatim(tmp_path, status, detail, expected):
    transport = real_app_transport(tmp_path, engine=FakeEngine(tmp_path, status=status, detail=detail))
    runner = SandboxDocxRunner(SandboxHttpClient(BASE, good_token, transport=transport, sleep=_no_sleep))
    assert await _failure(runner, "js") == expected


async def test_stub_engine_success_returns_the_engine_bytes(tmp_path):
    engine = FakeEngine(tmp_path, payload=b"PKstub")
    runner = SandboxDocxRunner(SandboxHttpClient(
        BASE, good_token, transport=real_app_transport(tmp_path, engine=engine), sleep=_no_sleep))
    assert await runner.run("js", _SPEC, 60) == b"PKstub"
    assert engine.calls[0][3].endswith(_SPEC.encode())


async def test_stub_engine_bad_request_is_charged_to_the_script(tmp_path):
    engine = FakeEngine(tmp_path, status=SandboxStatus.BAD_REQUEST, detail="request body or limits could not be used")
    runner = SandboxDocxRunner(SandboxHttpClient(
        BASE, good_token, transport=real_app_transport(tmp_path, engine=engine), sleep=_no_sleep))
    assert (await _failure(runner, "js")).startswith("exit code 4")
