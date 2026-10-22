"""ChildLauncher (RFC §4.6, §4.14): fresh child, own uid, allowlisted environment, whole-group wall clock kill."""
import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from src.adapters.sandbox_engine import child_launcher
from src.adapters.sandbox_engine.child_launcher import ChildLaunchError, ChildLauncher
from src.domain.sandbox_contract import SandboxLimits

_LINUX = sys.platform == "linux"
_PY = sys.executable
_ROOT = os.geteuid() == 0


def _limits(**overrides):
    values = dict(wall_s=30.0, memory_bytes=1 << 30, cpu_s=30.0, file_bytes=64 * 1024 * 1024)
    values.update(overrides)
    return SandboxLimits(**values)


def _launcher(**kw):
    if _ROOT:
        pytest.skip("these tests run children as the invoking user; under root the launcher (rightly) needs a user")
    kw.setdefault("nproc", 4000)   # per-uid count: the dev user already runs hundreds of processes
    kw.setdefault("nofile", 128)
    return ChildLauncher(**kw)


async def _run(launcher, tmp_path, code, *, limits=None, user=None, env=None):
    stdin = tmp_path / "in.bin"
    stdin.write_bytes(b"")
    out = tmp_path / "out.bin"
    result = await launcher.run(
        [_PY, "-c", code], workdir=str(tmp_path), env=env or {}, limits=limits or _limits(), user=user,
        stdin_path=str(stdin), stdout_path=str(out))
    return result, out


async def test_child_stdout_goes_to_the_file_and_stdin_comes_from_a_file(tmp_path):
    (tmp_path / "in.bin").write_bytes(b"payload")
    out = tmp_path / "out.bin"
    result = await _launcher().run(
        [_PY, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"],
        workdir=str(tmp_path), env={}, limits=_limits(), user=None,
        stdin_path=str(tmp_path / "in.bin"), stdout_path=str(out))
    assert result.returncode == 0 and not result.timed_out
    assert out.read_bytes() == b"daolyap"


async def test_peak_rss_is_reported(tmp_path):
    result, _ = await _run(_launcher(), tmp_path, "x = bytearray(30 * 1024 * 1024); x[::4096] = b'a' * len(x[::4096])")
    assert result.peak_rss_bytes > 10 * 1024 * 1024


async def test_child_environment_is_the_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEK_API_SECRET_KEY", "leak-me")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/creds.json")
    code = "import os,sys; sys.stdout.write(repr(dict(os.environ)))"
    result, out = await _run(_launcher(), tmp_path, code, env={"NODE_PATH": "/opt/modules"})
    assert result.returncode == 0
    seen = eval(out.read_text())   # noqa: S307 - our own child's output
    assert "leak-me" not in out.read_text()
    assert set(seen) <= set(child_launcher.ENV_ALLOWLIST) | {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert seen["NODE_PATH"] == "/opt/modules"
    assert seen["TMPDIR"] == str(tmp_path)
    assert seen["HOME"] == str(tmp_path)


async def test_a_caller_env_name_outside_the_allowlist_is_refused(tmp_path):
    with pytest.raises(ValueError):
        await _run(_launcher(), tmp_path, "pass", env={"API_KEY": "x"})


def _alive(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


@pytest.mark.slow
async def test_wall_clock_kills_the_process_group(tmp_path):
    code = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "open('grandchild.pid', 'w').write(str(g.pid))\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    result, _ = await _run(_launcher(), tmp_path, code, limits=_limits(wall_s=3.0))
    assert result.timed_out is True
    assert time.monotonic() - started < 30
    grandchild = int((tmp_path / "grandchild.pid").read_text())
    for _ in range(40):
        if not _alive(grandchild):
            break
        await asyncio.sleep(0.1)
    assert not _alive(grandchild)


@pytest.mark.slow
@pytest.mark.skipif(not _LINUX, reason="rlimit enforcement is verified on Linux only")
async def test_cpu_limit_kills_with_sigxcpu(tmp_path):
    result, _ = await _run(_launcher(), tmp_path, "while True: pass", limits=_limits(cpu_s=1.0, wall_s=30.0))
    assert result.returncode == -signal.SIGXCPU
    assert not result.timed_out


@pytest.mark.skipif(not _LINUX, reason="rlimit enforcement is verified on Linux only")
async def test_file_size_limit_enforced(tmp_path):
    code = (
        "import sys\n"
        "try:\n"
        "    open('big.bin', 'wb').write(b'x' * (4 * 1024 * 1024))\n"
        "except OSError:\n"
        "    sys.exit(7)\n"
    )
    result, _ = await _run(_launcher(), tmp_path, code, limits=_limits(file_bytes=1024 * 1024))
    assert result.returncode == 7
    assert (tmp_path / "big.bin").stat().st_size <= 1024 * 1024


@pytest.mark.skipif(not _LINUX, reason="rlimit enforcement is verified on Linux only")
async def test_nofile_limit_enforced(tmp_path):
    code = (
        "import sys\n"
        "held = []\n"
        "try:\n"
        "    for _ in range(200): held.append(open('/dev/null'))\n"
        "except OSError:\n"
        "    sys.exit(7 if len(held) < 40 else 8)\n"
    )
    result, _ = await _run(_launcher(nofile=32), tmp_path, code)
    assert result.returncode == 7


@pytest.mark.skipif(not _LINUX or _ROOT, reason="RLIMIT_NPROC is verified on Linux, and root bypasses it")
async def test_nproc_limit_enforced(tmp_path):
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    subprocess.Popen(['/bin/true']).wait()\n"
        "except OSError:\n"
        "    sys.exit(7)\n"
    )
    result, _ = await _run(_launcher(nproc=1), tmp_path, code)
    assert result.returncode == 7


async def test_stderr_is_summarised_not_returned(tmp_path, caplog):
    secret = "SECRET-STDERR-TEXT-" + "x" * 5000
    code = f"import sys; sys.stderr.write({secret!r})"
    with caplog.at_level(logging.DEBUG):
        result, _ = await _run(_launcher(), tmp_path, code)
    assert result.stderr_length == len(secret)
    assert result.stderr_sha256 == hashlib.sha256(secret.encode()).hexdigest()
    assert "SECRET-STDERR-TEXT" not in repr(result)
    assert "SECRET-STDERR-TEXT" not in caplog.text


async def test_stderr_hash_covers_output_beyond_any_buffer(tmp_path):
    code = "import sys; sys.stderr.write('a' * 3_000_000)"
    result, _ = await _run(_launcher(), tmp_path, code)
    assert result.stderr_length == 3_000_000
    assert result.stderr_sha256 == hashlib.sha256(b"a" * 3_000_000).hexdigest()


async def test_missing_configured_user_fails_start(tmp_path):
    with pytest.raises(ChildLaunchError):
        await _run(_launcher(), tmp_path, "pass", user="no-such-user-for-sandbox-tests")


async def test_root_as_configured_user_fails_start(tmp_path, monkeypatch):
    launcher = _launcher()
    monkeypatch.setattr(child_launcher, "_effective_uid", lambda: 0)   # past the "needs root" branch
    with pytest.raises(ChildLaunchError, match="is root"):
        await _run(launcher, tmp_path, "pass", user="root")


async def test_unswitchable_user_fails_start(tmp_path, monkeypatch):
    monkeypatch.setattr(child_launcher, "_effective_uid", lambda: 1000)
    with pytest.raises(ChildLaunchError):
        await _run(_launcher(), tmp_path, "pass", user="nobody")


class _FakeProc:
    pid = 4242
    returncode = 0

    async def wait(self):
        return 0


async def test_no_supplementary_groups_requested(tmp_path, monkeypatch):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    launcher = _launcher()
    monkeypatch.setattr(child_launcher, "_effective_uid", lambda: 0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await _run(launcher, tmp_path, "pass", user="nobody")
    assert seen["extra_groups"] == []
    assert seen["user"] == "nobody" or isinstance(seen["user"], int)
    assert seen["group"] is not None
    assert seen["start_new_session"] is True


async def test_no_identity_is_requested_without_a_configured_user(tmp_path, monkeypatch):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await _run(_launcher(), tmp_path, "pass", user=None)
    assert "user" not in seen and "extra_groups" not in seen


async def test_the_spawn_runs_inside_a_subprocess_memory_scope(tmp_path, monkeypatch):
    scopes = []
    from contextlib import contextmanager

    @contextmanager
    def fake_scope(kind, name, *a, **k):
        scopes.append((kind, name))
        yield

    monkeypatch.setattr(child_launcher, "memory_scope", fake_scope)
    stdin = tmp_path / "in.bin"
    stdin.write_bytes(b"")
    await _launcher().run([_PY, "-c", "pass"], workdir=str(tmp_path), env={}, limits=_limits(), user=None,
                          stdin_path=str(stdin), stdout_path=str(tmp_path / "o"), op="render_docx")
    assert scopes == [("subprocess", "sandbox_render_docx")]


def test_the_launcher_imports_no_parser_or_document_library():
    import ast
    source = open(child_launcher.__file__, encoding="utf-8").read()
    roots = {n.module.split(".")[0] for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom) and n.module}
    roots |= {a.name.split(".")[0] for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Import) for a in n.names}
    assert not roots & {"docx", "pypdf", "pdfminer", "markitdown", "bs4", "openpyxl", "PIL", "fitz"}
    assert not any("infrastructure" in (n.module or "") for n in ast.walk(ast.parse(source))
                   if isinstance(n, ast.ImportFrom))


@pytest.mark.docker
@pytest.mark.skipif(not os.getenv("SANDBOX_TEST_IMAGE") or shutil.which("docker") is None,
                    reason="needs the built render image (T9): set SANDBOX_TEST_IMAGE; run for S1 acceptance")
def test_uid_drop_in_container():
    """The engine child runs under its own uid, not the server's (S1 acceptance: I7)."""
    user = os.getenv("SANDBOX_TEST_USER", "sandbox")
    script = (
        "import asyncio, json, os, tempfile\n"
        "from src.adapters.sandbox_engine.child_launcher import ChildLauncher\n"
        "from src.domain.sandbox_contract import SandboxLimits\n"
        "import pwd\n"
        f"e = pwd.getpwnam({user!r})\n"
        "d = tempfile.mkdtemp(); os.chown(d, e.pw_uid, e.pw_gid); open(d + '/in', 'wb').close()\n"
        "async def main():\n"
        f"    r = await ChildLauncher(user={user!r}).run(['python3', '-c', 'import os; print(os.getuid())'],\n"
        "        workdir=d, env={},\n"
        f"        limits=SandboxLimits(30.0, 1 << 30, 30.0, 1 << 20), user={user!r}, stdin_path=d + '/in', stdout_path=d + '/out')\n"
        "    print(json.dumps({'server': os.getuid(), 'child': int(open(d + '/out').read()), 'rc': r.returncode}))\n"
        "asyncio.run(main())\n"
    )
    proc = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python3", os.environ["SANDBOX_TEST_IMAGE"],
                           "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    assert seen["rc"] == 0 and seen["child"] != seen["server"] and seen["child"] != 0


async def test_a_grandchild_outliving_a_clean_exit_is_killed(tmp_path):
    code = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "open('grandchild.pid', 'w').write(str(g.pid))\n"
    )
    result, _ = await _run(_launcher(), tmp_path, code)
    assert result.returncode == 0 and not result.timed_out
    grandchild = int((tmp_path / "grandchild.pid").read_text())
    for _ in range(40):
        if not _alive(grandchild):
            break
        await asyncio.sleep(0.1)
    assert not _alive(grandchild)


async def test_no_child_user_while_running_as_root_fails_start(tmp_path, monkeypatch):
    launcher = _launcher()
    monkeypatch.setattr(child_launcher, "_effective_uid", lambda: 0)
    with pytest.raises(ChildLaunchError, match="running as root"):
        await _run(launcher, tmp_path, "pass", user=None)


def test_a_misconfigured_user_fails_at_construction():
    with pytest.raises(ChildLaunchError):
        ChildLauncher(user="no-such-user-for-sandbox-tests")
    with pytest.raises(ChildLaunchError):
        ChildLauncher(user="root")


def test_root_without_a_user_fails_at_construction(monkeypatch):
    monkeypatch.setattr(child_launcher, "_effective_uid", lambda: 0)
    with pytest.raises(ChildLaunchError):
        ChildLauncher()


async def test_the_constructor_user_is_used_when_run_names_none(tmp_path, monkeypatch):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(child_launcher, "_effective_uid", lambda: 0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    launcher = _launcher(user="nobody")
    stdin = tmp_path / "in.bin"
    stdin.write_bytes(b"")
    await launcher.run([_PY, "-c", "pass"], workdir=str(tmp_path), env={}, limits=_limits(),
                       stdin_path=str(stdin), stdout_path=str(tmp_path / "o"))
    assert seen["user"] == "nobody" and seen["extra_groups"] == []


async def test_the_limits_reach_the_wrapper_unswapped(tmp_path, monkeypatch):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["argv"] = list(args)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    limits = SandboxLimits(wall_s=11.0, memory_bytes=123456789, cpu_s=7.0, file_bytes=987654)
    await _run(_launcher(nproc=77, nofile=55), tmp_path, "pass", limits=limits)
    argv = seen["argv"]
    spec = json.loads(argv[argv.index("--") - 1])
    assert (spec["memory_bytes"], spec["cpu_s"], spec["file_bytes"]) == (123456789, 7.0, 987654)
    assert (spec["nproc"], spec["nofile"], spec["cwd"]) == (77, 55, str(tmp_path))


async def test_a_failed_spawn_is_logged_before_it_propagates(tmp_path, caplog):
    stdin = tmp_path / "in.bin"
    stdin.write_bytes(b"")
    with caplog.at_level(logging.ERROR), pytest.raises(OSError):
        await _launcher(python="/nonexistent/python").run(
            ["x"], workdir=str(tmp_path), env={}, limits=_limits(), stdin_path=str(stdin),
            stdout_path=str(tmp_path / "o"))
    assert "could not be started" in caplog.text


@pytest.mark.slow
async def test_the_stderr_file_is_closed_when_the_run_is_cancelled(tmp_path, monkeypatch):
    import tempfile
    opened = []
    real = tempfile.TemporaryFile

    def recording(*a, **k):
        f = real(*a, **k)
        opened.append(f)
        return f

    monkeypatch.setattr(child_launcher.tempfile, "TemporaryFile", recording)
    task = asyncio.ensure_future(_run(_launcher(), tmp_path, "import time; time.sleep(60)",
                                      limits=_limits(wall_s=60.0)))
    for _ in range(100):      # wait until the child is running
        if opened:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened and all(f.closed for f in opened)


async def test_the_stderr_file_is_closed_when_the_spawn_fails(tmp_path, monkeypatch):
    import tempfile
    opened = []
    real = tempfile.TemporaryFile
    monkeypatch.setattr(child_launcher.tempfile, "TemporaryFile", lambda *a, **k: opened.append(real()) or opened[-1])
    with pytest.raises(OSError):
        await _launcher(python="/nonexistent/python").run(
            ["x"], workdir=str(tmp_path), env={}, limits=_limits(), stdin_path=str(tmp_path / "missing"),
            stdout_path=str(tmp_path / "o"))
    assert opened and all(f.closed for f in opened)
