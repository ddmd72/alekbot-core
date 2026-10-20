"""DocxRenderEngine (RFC §4.6, §4.9; plan G1, G2, G5, G20): mapping, caps, real-node behaviour."""
import asyncio
import json
import os
import shutil
import sys

import pytest

from src.adapters.sandbox_engine.child_launcher import ChildLauncher, ChildResult
from src.adapters.sandbox_engine.docx_render_engine import DocxRenderEngine
from src.domain.docx_script_prelude import SAFE_ENV_KEYS
from src.domain.sandbox_docx_frame import frame_body
from src.domain.sandbox_contract import ERROR_TEXT_CAP_BYTES, SandboxEngine, SandboxLimits, SandboxStatus

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_HAS_NODE = shutil.which("node") is not None and os.path.isdir(
    os.path.join(_ROOT_DIR, "docx_generator", "node_modules", "docx"))
needs_node = pytest.mark.skipif(
    not _HAS_NODE or os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
    reason="needs node + docx_generator/node_modules and a non-root user")

_OK_SCRIPT = b"""
const docx = require('docx');
let input = '';
process.stdin.on('data', d => input += d);
process.stdin.on('end', async () => {
  const spec = JSON.parse(input);
  const doc = new docx.Document({sections: [{children: [new docx.Paragraph(spec.title)]}]});
  process.stdout.write(await docx.Packer.toBuffer(doc));
});
"""


def _limits(**kw):
    values = dict(wall_s=30.0, memory_bytes=4 << 30, cpu_s=30.0, file_bytes=64 * 1024 * 1024)
    values.update(kw)
    return SandboxLimits(**values)


def _engine(**kw):
    return DocxRenderEngine(ChildLauncher(nproc=4000, nofile=256), **kw)


def _body(tmp_path, script: bytes, spec: bytes = b'{"title": "Hello"}'):
    path = tmp_path / "body"
    path.write_bytes(frame_body(script, spec))
    return str(path)


async def _run(tmp_path, script, spec=b'{"title": "Hello"}', **limit_kw):
    workdir = tmp_path / "work"
    workdir.mkdir()
    return await _engine().run(_body(tmp_path, script, spec), str(workdir), _limits(**limit_kw))


def test_satisfies_the_engine_protocol():
    assert isinstance(DocxRenderEngine(ChildLauncher(user=None)), SandboxEngine)


@needs_node
async def test_success_returns_docx_bytes(tmp_path):
    result = await _run(tmp_path, _OK_SCRIPT)
    assert result.status is SandboxStatus.OK, result.detail
    data = open(result.output_path, "rb").read()
    assert data[:2] == b"PK" and len(data) > 1000


@needs_node
async def test_script_error_text_matches_inprocess_format(tmp_path):
    result = await _run(tmp_path, b"throw new Error('boom-marker');")
    assert result.status is SandboxStatus.SCRIPT_ERROR
    assert result.detail.startswith("exit code 1\n")
    assert "boom-marker" in result.detail


@needs_node
async def test_no_stderr_placeholder(tmp_path):
    result = await _run(tmp_path, b"process.exit(3);")
    assert result.status is SandboxStatus.SCRIPT_ERROR
    assert result.detail == "exit code 3\n(no stderr)"


@needs_node
async def test_timeout_maps_to_timeout_text(tmp_path):
    result = await _run(tmp_path, b"setInterval(() => {}, 1000);", wall_s=2.0)
    assert result.status is SandboxStatus.TIMEOUT
    assert result.detail == "Node.js process timed out after 2s"


@needs_node
async def test_empty_stdout_maps_to_empty_output(tmp_path):
    result = await _run(tmp_path, b"1+1;")
    assert result.status is SandboxStatus.EMPTY_OUTPUT
    assert result.detail == "Node.js exited 0 but stdout is empty — no DOCX bytes produced"
    assert result.output_path is None


@needs_node
@pytest.mark.parametrize("module", ["fs", "child_process", "net", "node:fs"])
async def test_blocked_module_still_blocked(tmp_path, module):
    result = await _run(tmp_path, f"require('{module}');".encode())
    assert result.status is SandboxStatus.SCRIPT_ERROR
    assert "Blocked module for security" in result.detail


@needs_node
async def test_env_has_no_secret_names(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_TEST_SECRET_TOKEN", "s3cret-value")
    script = b"process.stdout.write(JSON.stringify(Object.keys(process.env)));"
    result = await _run(tmp_path, script)
    assert result.status is SandboxStatus.OK, result.detail
    text = open(result.output_path).read()
    assert "SANDBOX_TEST_SECRET_TOKEN" not in text and "s3cret-value" not in text
    # macOS's runtime adds __CF_USER_TEXT_ENCODING to every process on its own; it is not ours to withhold.
    assert set(json.loads(text)) - {"__CF_USER_TEXT_ENCODING"} <= set(SAFE_ENV_KEYS)
    assert "NODE_PATH" in text


# -- fake launcher: no node ---------------------------------------------------------------------------------------

class _FakeLauncher:
    """Plays the child: runs `behaviour(workdir)` and returns the canned ChildResult."""
    def __init__(self, behaviour, returncode=0, timed_out=False):
        self._behaviour, self._rc, self._timed_out = behaviour, returncode, timed_out
        self.calls = []

    async def run(self, argv, **kw):
        self.calls.append((list(argv), kw))
        self._behaviour(kw["workdir"])
        return ChildResult(returncode=self._rc, timed_out=self._timed_out, peak_rss_bytes=123,
                           stderr_length=0, stderr_sha256="0" * 64)


async def _fake_run(tmp_path, behaviour, **kw):
    workdir = tmp_path / "work"
    workdir.mkdir()
    launcher = _FakeLauncher(behaviour, **{k: v for k, v in kw.items() if k in ("returncode", "timed_out")})
    engine = DocxRenderEngine(launcher, **{k: v for k, v in kw.items() if k == "response_cap_bytes"})
    result = await engine.run(str(tmp_path / "body"), str(workdir), _limits())
    return result, launcher


async def test_error_text_is_capped(tmp_path):
    huge = "E" * (ERROR_TEXT_CAP_BYTES * 4)
    result, _ = await _fake_run(
        tmp_path, lambda w: open(os.path.join(w, "err.txt"), "w").write(huge), returncode=1)
    assert result.status is SandboxStatus.SCRIPT_ERROR
    assert result.detail.startswith("exit code 1\n")
    assert len(result.detail.encode()) <= ERROR_TEXT_CAP_BYTES
    assert "truncated" in result.detail


async def test_over_cap_output_is_too_large(tmp_path):
    def write(w):
        with open(os.path.join(w, "out.bin"), "wb") as f:
            f.write(b"x" * 11)
    result, _ = await _fake_run(tmp_path, write, response_cap_bytes=10)
    assert result.status is SandboxStatus.TOO_LARGE
    assert result.output_path is None


async def test_launch_arguments(tmp_path):
    def write(w):
        open(os.path.join(w, "out.bin"), "wb").write(b"PK")
    result, launcher = await _fake_run(tmp_path, write)
    argv, kw = launcher.calls[0]
    assert result.status is SandboxStatus.OK and result.peak_rss_bytes == 123
    assert os.path.dirname(result.output_path) != str(tmp_path / "work")   # outside the engine-writable workdir
    assert open(result.output_path, "rb").read() == b"PK"
    assert argv[-2:] == [str(tmp_path / "body"), str(tmp_path / "work")]
    assert argv[1:3] == ["-B", "-I"] and argv[3].endswith("render_docx_child.py") and "limits_exec" not in " ".join(argv)
    assert kw["op"] == "render_docx" and kw["env"] == {}
    assert kw["stdin_path"] == os.devnull and kw["stdout_path"] == os.devnull


async def test_child_failure_before_node_is_not_a_script_error(tmp_path):
    result, _ = await _fake_run(tmp_path, lambda w: None, returncode=4)
    assert result.status is SandboxStatus.BAD_REQUEST
    other = tmp_path / "other"
    other.mkdir()
    result, _ = await _fake_run(other, lambda w: None, returncode=5)
    assert result.status is SandboxStatus.ERROR


async def test_file_size_signal_maps_to_too_large(tmp_path):
    import signal
    result, _ = await _fake_run(tmp_path, lambda w: None, returncode=-signal.SIGXFSZ)
    assert result.status is SandboxStatus.TOO_LARGE


# -- hostile workdir: the script controls err.txt / out.bin ---------------------------------------------------------

_SECRET = b"SERVER-ONLY-SECRET-BYTES"


def _target(tmp_path):
    target = tmp_path / "server_secret"
    target.write_bytes(_SECRET)
    return str(target)


def _in(workdir, name):
    return os.path.join(workdir, name)


async def test_err_txt_symlink_does_not_leak(tmp_path):
    target = _target(tmp_path)
    result, _ = await _fake_run(tmp_path, lambda w: os.symlink(target, _in(w, "err.txt")), returncode=1)
    assert result.status is SandboxStatus.ERROR
    assert _SECRET.decode() not in result.detail


async def test_out_bin_symlink_is_refused(tmp_path):
    target = _target(tmp_path)
    result, _ = await _fake_run(tmp_path, lambda w: os.symlink(target, _in(w, "out.bin")))
    assert result.status is not SandboxStatus.OK and result.output_path is None
    assert _SECRET.decode() not in result.detail


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
async def test_out_bin_fifo_returns_promptly(tmp_path):
    result, _ = await asyncio.wait_for(
        _fake_run(tmp_path, lambda w: os.mkfifo(_in(w, "out.bin"))), timeout=5)
    assert result.status is not SandboxStatus.OK and result.output_path is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
async def test_err_txt_fifo_returns_promptly(tmp_path):
    result, _ = await asyncio.wait_for(
        _fake_run(tmp_path, lambda w: os.mkfifo(_in(w, "err.txt")), returncode=1), timeout=5)
    assert result.status is SandboxStatus.ERROR


async def test_out_bin_directory_is_refused(tmp_path):
    result, _ = await _fake_run(tmp_path, lambda w: os.mkdir(_in(w, "out.bin")))
    assert result.status is not SandboxStatus.OK and result.output_path is None


async def test_output_dir_is_private_and_outside_workdir(tmp_path):
    def write(w):
        open(_in(w, "out.bin"), "wb").write(b"PK")
    result, _ = await _fake_run(tmp_path, write)
    out_dir = os.path.dirname(result.output_path)
    assert not out_dir.startswith(str(tmp_path / "work"))
    assert (os.stat(out_dir).st_mode & 0o777) == 0o700
