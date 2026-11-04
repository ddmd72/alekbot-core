"""HtmlRenderEngine (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6-§4.8; slice S2): every exit path answers a typed status with a fixed
detail code (never engine text), out.bin is read the docx engine's way and only a copy outside the workdir leaves. A stub
child (render_html_child_stub.py) stands in for render_html_child under the real ChildLauncher; the real browser runs in
test_html_render_engine_real.py."""
import json
import os
import signal
import sys

import pytest

from src.adapters.sandbox_engine import html_render_engine as engine_module
from src.adapters.sandbox_engine.child_launcher import ENV_ALLOWLIST, ChildLauncher, ChildResult
from src.adapters.sandbox_engine.html_render_engine import (
    BROWSER_MEMORY_MODE, BROWSER_NOFILE, BROWSER_NPROC, CHROMIUM_OWN_SANDBOX, HtmlRenderEngine,
)
from src.adapters.sandbox_engine.limits_exec import NPROC_DEFAULT
from src.domain.sandbox_contract import SandboxEngine, SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus

needs_children = pytest.mark.skipif(
    os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
    reason="children run as the invoking user: needs a non-root user")

_STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_html_child_stub.py")
_CACHE_DIR = "/opt/puppeteer"
# The engine's fixed detail codes: the only text a non-ok answer may carry.
_DETAIL_CODES = {"wall_clock", "cpu_limit", "file_size_limit", "response_cap", "bad_request", "engine_exec_failed",
                 "browser_unavailable", "render_failed", "engine_killed", "engine_exit", "empty_output",
                 "output_not_regular", "workdir_unavailable"}


def _limits(**kw):
    values = dict(wall_s=30.0, memory_bytes=1 << 30, cpu_s=30.0, file_bytes=64 * 1024 * 1024)
    values.update(kw)
    return SandboxLimits(**values)


class Rig:
    """One request: a workdir with the body (the stub's behaviour) in it, an output root, and the engine for one op."""

    def __init__(self, tmp_path, behaviour="ok", *, op=SandboxOp.RENDER_PDF, launcher=None, **engine_kw):
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        self.out_root = tmp_path / "out"
        self.out_root.mkdir(mode=0o700)
        self.body = tmp_path / "body"
        self.body.write_bytes(behaviour.encode())
        self.engine = HtmlRenderEngine(
            launcher or ChildLauncher(nproc=4000, nofile=256), op=op, output_root=str(self.out_root),
            browser_cache_dir=_CACHE_DIR, child_script=_STUB, **engine_kw)

    async def run(self, **limit_kw):
        limits = _limits(**limit_kw)
        return await self.engine.run(str(self.body), str(self.workdir), limits,
                                     SandboxMeta(request_id="r" * 16, limits=limits))


async def _answer(tmp_path, behaviour, **kw):
    limit_kw = {k: kw.pop(k) for k in list(kw) if k in ("wall_s", "cpu_s")}
    rig = Rig(tmp_path, behaviour, **kw)
    return await rig.run(**limit_kw), rig


# -- construction -------------------------------------------------------------------------------------------------

def test_satisfies_the_engine_protocol(tmp_path):
    engine = HtmlRenderEngine(ChildLauncher(user=None), op=SandboxOp.RENDER_PNG, output_root=None,
                              browser_cache_dir=_CACHE_DIR)
    assert isinstance(engine, SandboxEngine)


@pytest.mark.parametrize("op", [SandboxOp.RENDER_DOCX, SandboxOp.PARSE_EXTRACT])
def test_refuses_an_op_that_is_not_an_html_render(op):
    with pytest.raises(ValueError):
        HtmlRenderEngine(ChildLauncher(user=None), op=op, output_root=None, browser_cache_dir=_CACHE_DIR)


def test_refuses_an_empty_browser_cache_dir():
    with pytest.raises(ValueError):
        HtmlRenderEngine(ChildLauncher(user=None), op=SandboxOp.RENDER_PDF, output_root=None, browser_cache_dir="")


def test_refuses_an_output_root_open_to_others(tmp_path):
    root = tmp_path / "open"
    root.mkdir()
    root.chmod(0o777)
    with pytest.raises(ValueError):
        HtmlRenderEngine(ChildLauncher(user=None), op=SandboxOp.RENDER_PDF, output_root=str(root),
                         browser_cache_dir=_CACHE_DIR)


def test_browser_family_constants():
    """D2: RLIMIT_DATA, not AS; NPROC and NOFILE above the S1 defaults; Chromium's own sandbox off until S0-f."""
    assert (BROWSER_MEMORY_MODE, BROWSER_NPROC, BROWSER_NOFILE) == ("data", 512, 1024)
    assert BROWSER_NPROC > NPROC_DEFAULT
    assert CHROMIUM_OWN_SANDBOX is False


# -- launch arguments and environment -----------------------------------------------------------------------------

@needs_children
@pytest.mark.parametrize("op, mode", [(SandboxOp.RENDER_PDF, "pdf"), (SandboxOp.RENDER_PNG, "png")])
@pytest.mark.parametrize("own_sandbox, flag", [(False, "0"), (True, "1")])
async def test_child_argv_carries_mode_cache_dir_and_sandbox_flag(tmp_path, op, mode, own_sandbox, flag):
    result, rig = await _answer(tmp_path, "echo", op=op, chromium_sandbox=own_sandbox)
    assert result.status is SandboxStatus.OK, result.detail
    seen = json.loads(open(result.output_path, "rb").read())
    assert seen["argv"] == [str(rig.body), str(rig.workdir), mode, _CACHE_DIR, flag]


@needs_children
async def test_child_environment_has_only_allowlisted_names(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_TEST_SECRET_TOKEN", "s3cret-value")
    result, _ = await _answer(tmp_path, "echo")
    assert result.status is SandboxStatus.OK, result.detail
    seen = json.loads(open(result.output_path, "rb").read())
    # macOS's runtime adds __CF_USER_TEXT_ENCODING to every process on its own; it is not ours to withhold.
    assert set(seen["env"]) - {"__CF_USER_TEXT_ENCODING"} <= set(ENV_ALLOWLIST)
    assert "SANDBOX_TEST_SECRET_TOKEN" not in seen["env"]


class _RecordingLauncher:
    def __init__(self, returncode=0, timed_out=False):
        self._rc, self._timed_out = returncode, timed_out
        self.calls = []

    async def run(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return ChildResult(returncode=self._rc, timed_out=self._timed_out, peak_rss_bytes=321,
                           stderr_length=7, stderr_sha256="0" * 64)


async def test_launch_keywords(tmp_path):
    launcher = _RecordingLauncher(returncode=4)
    rig = Rig(tmp_path, op=SandboxOp.RENDER_PNG, launcher=launcher, python="/usr/bin/python3")
    result = await rig.run(wall_s=12.0)
    argv, kw = launcher.calls[0]
    assert argv[:4] == ["/usr/bin/python3", "-B", "-I", _STUB]
    assert kw["env"] == {} and kw["workdir"] == str(rig.workdir) and kw["limits"].wall_s == 12.0
    assert kw["stdin_path"] == os.devnull and kw["stdout_path"] == os.devnull and kw["op"] == "render_png"
    assert result.peak_rss_bytes == 321


def test_the_real_child_is_the_default_script():
    assert engine_module._CHILD_SCRIPT.endswith(os.path.join("sandbox_engine", "render_html_child.py"))
    assert os.path.isfile(engine_module._CHILD_SCRIPT)


# -- status map ---------------------------------------------------------------------------------------------------

@needs_children
async def test_ok_output_is_copied_outside_the_workdir(tmp_path):
    result, rig = await _answer(tmp_path, "ok")
    assert result.status is SandboxStatus.OK and result.detail == ""
    assert os.path.dirname(os.path.dirname(result.output_path)) == str(rig.out_root)
    assert not result.output_path.startswith(str(rig.workdir))
    assert open(result.output_path, "rb").read() == b"%PDF-stub"


@needs_children
@pytest.mark.parametrize("behaviour, status, detail", [
    ("exit1", SandboxStatus.SCRIPT_ERROR, "render_failed"),
    ("exit2", SandboxStatus.ERROR, "browser_unavailable"),
    ("exit4", SandboxStatus.BAD_REQUEST, "bad_request"),
    ("exit5", SandboxStatus.ERROR, "engine_exec_failed"),
    ("exit9", SandboxStatus.ERROR, "engine_exit"),
    ("signal:SIGXFSZ", SandboxStatus.TOO_LARGE, "file_size_limit"),
    ("signal:SIGXCPU", SandboxStatus.TIMEOUT, "cpu_limit"),
    ("signal:SIGKILL", SandboxStatus.ERROR, "engine_killed"),
    ("signal:SIGSEGV", SandboxStatus.ERROR, "engine_killed"),
    ("empty", SandboxStatus.EMPTY_OUTPUT, "empty_output"),
    ("missing", SandboxStatus.EMPTY_OUTPUT, "empty_output"),
    ("symlink", SandboxStatus.ERROR, "output_not_regular"),
    ("hardlink", SandboxStatus.ERROR, "output_not_regular"),
    ("fifo", SandboxStatus.ERROR, "output_not_regular"),
])
async def test_status_map(tmp_path, behaviour, status, detail):
    result, rig = await _answer(tmp_path, behaviour)
    assert (result.status, result.detail) == (status, detail)
    assert result.output_path is None
    assert os.listdir(rig.out_root) == []


@needs_children
async def test_output_over_the_response_cap_is_too_large(tmp_path):
    result, rig = await _answer(tmp_path, "big", response_cap_bytes=1024)
    assert (result.status, result.detail, result.output_path) == (SandboxStatus.TOO_LARGE, "response_cap", None)
    assert os.listdir(rig.out_root) == []


@needs_children
async def test_output_at_the_response_cap_is_ok(tmp_path):
    result, _ = await _answer(tmp_path, "big", response_cap_bytes=4096)
    assert result.status is SandboxStatus.OK
    assert os.path.getsize(result.output_path) == 4096


async def test_wall_clock_maps_to_timeout_without_reading_output(tmp_path):
    rig = Rig(tmp_path, launcher=_RecordingLauncher(returncode=-signal.SIGKILL, timed_out=True))
    (rig.workdir / "out.bin").write_bytes(b"%PDF-partial")
    result = await rig.run()
    assert (result.status, result.detail, result.output_path) == (SandboxStatus.TIMEOUT, "wall_clock", None)


@pytest.mark.slow
@needs_children
async def test_wall_clock_kills_a_hanging_child(tmp_path):
    result, _ = await _answer(tmp_path, "sleep", wall_s=0.5)
    assert (result.status, result.detail, result.output_path) == (SandboxStatus.TIMEOUT, "wall_clock", None)


async def test_a_vanished_workdir_is_an_error(tmp_path):
    rig = Rig(tmp_path, launcher=_RecordingLauncher(returncode=0))
    rig.workdir.rmdir()
    result = await rig.run()
    assert (result.status, result.detail) == (SandboxStatus.ERROR, "workdir_unavailable")


async def test_non_ok_details_are_fixed_codes_only(tmp_path):
    """No engine or browser text: every non-ok detail is one of the fixed codes (§2: the client maps statuses)."""
    for code in (1, 2, 4, 5, 7, -signal.SIGXFSZ, -signal.SIGXCPU, -signal.SIGKILL):
        case = tmp_path / str(code).replace("-", "m")
        case.mkdir()
        result = await Rig(case, launcher=_RecordingLauncher(returncode=code)).run()
        assert result.status is not SandboxStatus.OK and result.detail in _DETAIL_CODES, (code, result.detail)
