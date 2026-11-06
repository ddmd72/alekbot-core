"""render.docx under the production limits (G6 / S0-f).

Prod 2026-11-06: every real model script ended rc=-6 with an empty err.txt. The scripts read their spec from stdin (a
file), which starts libuv's threadpool; under RLIMIT_AS 1 GiB its threads could not get their stacks and libuv aborts
without a message. The engine's launcher now bounds RLIMIT_DATA instead (`DOCX_MEMORY_MODE`).

Two levels:
- real node on the developer's machine, production memory / CPU / file / NOFILE limits in the production memory mode.
  NPROC stays high: RLIMIT_NPROC counts every process of the developer's own uid, not only node's threads. macOS refuses
  RLIMIT_DATA (limits_exec tolerates it there), so the limit assertion runs on Linux only; CI has no node_modules and skips.
- docker: the built render image, the production registry (`build_engine_registry`), the `sandboxengine` uid and every
  production limit, NPROC 64 included. Skipped unless SANDBOX_TEST_IMAGE is set (S1 acceptance).
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.docx_render_engine import DOCX_MEMORY_MODE, DocxRenderEngine
from src.adapters.sandbox_engine.limits_exec import NOFILE_DEFAULT
from src.domain.sandbox_contract import (
    CEILING_CPU_S, CEILING_FILE_BYTES, CEILING_MEMORY_BYTES, SandboxLimits, SandboxMeta, SandboxStatus,
)
from src.domain.sandbox_docx_frame import frame_body

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_HAS_NODE = shutil.which("node") is not None and os.path.isdir(
    os.path.join(_ROOT_DIR, "docx_generator", "node_modules", "docx"))
needs_node = pytest.mark.skipif(
    not _HAS_NODE or os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
    reason="needs node + docx_generator/node_modules and a non-root user")

# The shape of the failing model scripts: spec from a stdin stream (libuv threadpool), then require('docx') + Packer.
STDIN_STREAM_SCRIPT = """
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', async () => {
  const { Document, Packer, Paragraph, Table, TableRow, TableCell, TextRun } = require('docx');
  const spec = JSON.parse(raw);
  const rows = Array.from({ length: 30 }, (_, i) => new TableRow({ children: [
    new TableCell({ children: [new Paragraph(spec.title + ' ' + i)] }),
    new TableCell({ children: [new Paragraph({ children: [new TextRun({ text: 'Hello', bold: true })] })] }),
  ] }));
  const doc = new Document({ sections: [{ children: [new Paragraph(spec.title), new Table({ rows })] }] });
  process.stdout.write(await Packer.toBuffer(doc));
});
"""

LIMITS_REPORT_SCRIPT = """
let raw = '';
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', () => {
  const limits = process.report.getReport().userLimits;
  process.stdout.write(JSON.stringify({ data: limits.data_seg_size_kbytes, vm: limits.virtual_memory_kbytes }));
});
"""


def _production_limits():
    return SandboxLimits(wall_s=60.0, memory_bytes=CEILING_MEMORY_BYTES, cpu_s=CEILING_CPU_S,
                         file_bytes=CEILING_FILE_BYTES)


async def _run(tmp_path, script: str):
    workdir = tmp_path / "work"
    workdir.mkdir()
    body = tmp_path / "body"
    body.write_bytes(frame_body(script.encode(), b'{"title": "Hello"}'))
    engine = DocxRenderEngine(ChildLauncher(memory_mode=DOCX_MEMORY_MODE, nproc=4000, nofile=NOFILE_DEFAULT),
                              output_root=str(_private_dir(tmp_path / "out")))
    limits = _production_limits()
    return await engine.run(str(body), str(workdir), limits, SandboxMeta(request_id="r" * 16, limits=limits))


def _private_dir(path):
    path.mkdir(mode=0o700)
    return path


@needs_node
async def test_stdin_stream_docx_script_renders_under_production_limits(tmp_path):
    result = await _run(tmp_path, STDIN_STREAM_SCRIPT)
    assert result.status == SandboxStatus.OK, result.detail
    with open(result.output_path, "rb") as handle:
        assert handle.read(2) == b"PK"


@needs_node
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="macOS refuses RLIMIT_DATA / RLIMIT_AS")
async def test_node_runs_with_the_data_limit_and_no_address_space_limit(tmp_path):
    result = await _run(tmp_path, LIMITS_REPORT_SCRIPT)
    assert result.status == SandboxStatus.OK, result.detail
    with open(result.output_path, "rb") as handle:
        seen = json.loads(handle.read())
    assert seen["data"]["soft"] == CEILING_MEMORY_BYTES // 1024
    assert seen["vm"]["soft"] == "unlimited"


_IMAGE_SCRIPT = """
import asyncio, json, os, pwd, tempfile
from src.composition.sandbox_setup import build_engine_registry, validate_startup
from src.domain.sandbox_contract import (
    CEILING_CPU_S, CEILING_FILE_BYTES, CEILING_MEMORY_BYTES, SandboxLimits, SandboxMeta, SandboxOp,
)
from src.domain.sandbox_docx_frame import frame_body

plan = validate_startup({"SANDBOX_SERVICE": "render", "SANDBOX_CALLERS_RENDER": "caller@example.com",
                         "SANDBOX_ENGINE_USER": "sandboxengine", "SANDBOX_WORK_ROOT": "/work"}, on_cloud_run=False)
engine = build_engine_registry(plan)[SandboxOp.RENDER_DOCX]
ids = pwd.getpwnam("sandboxengine")
workdir = tempfile.mkdtemp(dir="/work")
os.chown(workdir, ids.pw_uid, ids.pw_gid)
body = os.path.join(workdir, "body")
with open(body, "wb") as handle:
    handle.write(frame_body(os.environ["DOCX_SCRIPT"].encode(), b'{"title": "Hello"}'))
os.chown(body, ids.pw_uid, ids.pw_gid)
limits = SandboxLimits(60.0, CEILING_MEMORY_BYTES, CEILING_CPU_S, CEILING_FILE_BYTES)
result = asyncio.run(engine.run(body, workdir, limits, SandboxMeta(request_id="r" * 16, limits=limits)))
head = open(result.output_path, "rb").read(2).decode("latin-1") if result.output_path else ""
print(json.dumps({"status": result.status.value, "detail": result.detail, "head": head}))
"""


@pytest.mark.docker
@pytest.mark.skipif(not os.getenv("SANDBOX_TEST_IMAGE") or shutil.which("docker") is None,
                    reason="needs the built render image: set SANDBOX_TEST_IMAGE; run for S1 acceptance")
def test_stdin_stream_docx_script_renders_in_the_image_under_every_production_limit():
    proc = subprocess.run(
        ["docker", "run", "--rm", "-e", "DOCX_SCRIPT", "--entrypoint", "python", os.environ["SANDBOX_TEST_IMAGE"],
         "-c", _IMAGE_SCRIPT],
        capture_output=True, text=True, timeout=180, env={**os.environ, "DOCX_SCRIPT": STDIN_STREAM_SCRIPT})
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    assert seen == {"status": "ok", "detail": "", "head": "PK"}, seen
