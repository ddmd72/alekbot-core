"""
Wire tests for NodePuppeteerRunner.

Mock boundary: asyncio.create_subprocess_exec (subprocess layer).
Never mock at PuppeteerRunnerPort level — that hides translation bugs.

Covers:
- runner.js not found → PuppeteerRunnerError
- HTML bytes sent to subprocess stdin
- PDF bytes captured from subprocess stdout
- Non-zero exit code → PuppeteerRunnerError with exit code
- Timeout → PuppeteerRunnerError, process killed
- Empty stdout → PuppeteerRunnerError
- Non-fatal stderr logged but not raised on success
- cwd set to pdf_generator directory
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from src.adapters.node_puppeteer_runner import NodePuppeteerRunner, _PDF_GENERATOR_DIR, _RUNNER_SCRIPT
from src.ports.puppeteer_runner_port import PuppeteerRunnerError


# ============================================================================
# Helpers
# ============================================================================

_FAKE_PDF = b"%PDF-1.4 fake-pdf-content"
_FAKE_HTML = "<html><body><h1>Test</h1></body></html>"


def _make_proc(returncode=0, stdout=_FAKE_PDF, stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


def _patch_runner_exists(exists=True):
    return patch("src.adapters.node_puppeteer_runner.os.path.isfile", return_value=exists)


def _patch_subprocess(proc):
    return patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    )


# ============================================================================
# runner.js existence check
# ============================================================================

async def test_missing_runner_script_raises_before_subprocess():
    with _patch_runner_exists(False):
        runner = NodePuppeteerRunner()
        with pytest.raises(PuppeteerRunnerError, match="runner.js not found"):
            await runner.run(_FAKE_HTML, timeout=60)


async def test_missing_runner_script_error_mentions_npm_install():
    with _patch_runner_exists(False):
        runner = NodePuppeteerRunner()
        with pytest.raises(PuppeteerRunnerError, match="npm install"):
            await runner.run(_FAKE_HTML, timeout=60)


# ============================================================================
# Success path
# ============================================================================

async def test_success_returns_pdf_bytes():
    proc = _make_proc(stdout=_FAKE_PDF)
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        result = await runner.run(_FAKE_HTML, timeout=60)

    assert result == _FAKE_PDF


async def test_success_sends_html_as_utf8_to_stdin():
    proc = _make_proc()
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        await runner.run(_FAKE_HTML, timeout=60)

    proc.communicate.assert_called_once_with(input=_FAKE_HTML.encode("utf-8"))


async def test_success_calls_node_with_runner_script():
    proc = _make_proc()
    with _patch_runner_exists():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            runner = NodePuppeteerRunner()
            await runner.run(_FAKE_HTML, timeout=60)

    args = mock_exec.call_args.args
    assert args[0] == "node"
    assert args[1] == _RUNNER_SCRIPT


async def test_success_sets_cwd_to_pdf_generator_dir():
    proc = _make_proc()
    with _patch_runner_exists():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            runner = NodePuppeteerRunner()
            await runner.run(_FAKE_HTML, timeout=60)

    kwargs = mock_exec.call_args.kwargs
    assert kwargs.get("cwd") == _PDF_GENERATOR_DIR


async def test_success_with_stderr_does_not_raise():
    proc = _make_proc(stderr=b"Puppeteer: browser launched")
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        result = await runner.run(_FAKE_HTML, timeout=60)

    assert result == _FAKE_PDF  # non-fatal stderr ignored


# ============================================================================
# Non-zero exit code
# ============================================================================

async def test_nonzero_exit_raises_puppeteer_error():
    proc = _make_proc(returncode=1, stderr=b"Error: page crash")
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        with pytest.raises(PuppeteerRunnerError):
            await runner.run(_FAKE_HTML, timeout=60)


async def test_nonzero_exit_error_contains_exit_code():
    proc = _make_proc(returncode=2, stderr=b"Fatal error")
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        with pytest.raises(PuppeteerRunnerError, match="exit code 2"):
            await runner.run(_FAKE_HTML, timeout=60)


async def test_nonzero_exit_error_contains_stderr():
    proc = _make_proc(returncode=1, stderr=b"ENOENT: node not found")
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        with pytest.raises(PuppeteerRunnerError, match="ENOENT"):
            await runner.run(_FAKE_HTML, timeout=60)


async def test_nonzero_exit_no_stderr_still_raises():
    proc = _make_proc(returncode=1, stderr=b"")
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        with pytest.raises(PuppeteerRunnerError, match="exit code 1"):
            await runner.run(_FAKE_HTML, timeout=60)


# ============================================================================
# Timeout
# ============================================================================

async def test_timeout_raises_puppeteer_error():
    proc = _make_proc()
    with _patch_runner_exists(), _patch_subprocess(proc):
        with patch(
            "asyncio.wait_for",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ):
            runner = NodePuppeteerRunner()
            with pytest.raises(PuppeteerRunnerError, match="timed out"):
                await runner.run(_FAKE_HTML, timeout=30)


async def test_timeout_error_contains_timeout_value():
    proc = _make_proc()
    with _patch_runner_exists(), _patch_subprocess(proc):
        with patch(
            "asyncio.wait_for",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ):
            runner = NodePuppeteerRunner()
            with pytest.raises(PuppeteerRunnerError, match="30s"):
                await runner.run(_FAKE_HTML, timeout=30)


async def test_timeout_kills_process():
    proc = _make_proc()
    with _patch_runner_exists(), _patch_subprocess(proc):
        with patch(
            "asyncio.wait_for",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ):
            runner = NodePuppeteerRunner()
            with pytest.raises(PuppeteerRunnerError):
                await runner.run(_FAKE_HTML, timeout=30)

    proc.kill.assert_called_once()


async def test_timeout_waits_for_process_after_kill():
    proc = _make_proc()
    with _patch_runner_exists(), _patch_subprocess(proc):
        with patch(
            "asyncio.wait_for",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ):
            runner = NodePuppeteerRunner()
            with pytest.raises(PuppeteerRunnerError):
                await runner.run(_FAKE_HTML, timeout=30)

    proc.wait.assert_called_once()


# ============================================================================
# Empty stdout
# ============================================================================

async def test_empty_stdout_raises_puppeteer_error():
    proc = _make_proc(returncode=0, stdout=b"")
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        with pytest.raises(PuppeteerRunnerError, match="empty"):
            await runner.run(_FAKE_HTML, timeout=60)


async def test_nonempty_stdout_does_not_raise():
    proc = _make_proc(returncode=0, stdout=b"x")
    with _patch_runner_exists(), _patch_subprocess(proc):
        runner = NodePuppeteerRunner()
        result = await runner.run(_FAKE_HTML, timeout=60)

    assert result == b"x"


# ============================================================================
# stdin/stdout pipes configured
# ============================================================================

async def test_subprocess_stdout_pipe_configured():
    proc = _make_proc()
    with _patch_runner_exists():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            runner = NodePuppeteerRunner()
            await runner.run(_FAKE_HTML, timeout=60)

    kwargs = mock_exec.call_args.kwargs
    assert kwargs.get("stdout") == asyncio.subprocess.PIPE


async def test_subprocess_stdin_pipe_configured():
    proc = _make_proc()
    with _patch_runner_exists():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            runner = NodePuppeteerRunner()
            await runner.run(_FAKE_HTML, timeout=60)

    kwargs = mock_exec.call_args.kwargs
    assert kwargs.get("stdin") == asyncio.subprocess.PIPE


async def test_subprocess_stderr_pipe_configured():
    proc = _make_proc()
    with _patch_runner_exists():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            runner = NodePuppeteerRunner()
            await runner.run(_FAKE_HTML, timeout=60)

    kwargs = mock_exec.call_args.kwargs
    assert kwargs.get("stderr") == asyncio.subprocess.PIPE


# ============================================================================
# Sandbox: subprocess env must carry no application secrets
# ============================================================================

async def test_subprocess_env_excludes_secrets(monkeypatch):
    """The browser renders untrusted LLM HTML — its env must not leak secrets."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-should-not-leak")
    monkeypatch.setenv("OAUTH_SESSION_SECRET", "super-secret-signing-key")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    proc = _make_proc()
    with _patch_runner_exists():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            runner = NodePuppeteerRunner()
            await runner.run(_FAKE_HTML, timeout=60)

    env = mock_exec.call_args.kwargs["env"]
    assert env.get("PATH") == "/usr/bin:/bin"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OAUTH_SESSION_SECRET" not in env


async def test_subprocess_env_keeps_puppeteer_keys(monkeypatch):
    """PUPPETEER_* config keys survive so a custom Chromium path still resolves."""
    monkeypatch.setenv("PUPPETEER_EXECUTABLE_PATH", "/opt/chrome/chrome")
    monkeypatch.setenv("PATH", "/usr/bin")
    proc = _make_proc()
    with _patch_runner_exists():
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            runner = NodePuppeteerRunner()
            await runner.run(_FAKE_HTML, timeout=60)

    env = mock_exec.call_args.kwargs["env"]
    assert env.get("PUPPETEER_EXECUTABLE_PATH") == "/opt/chrome/chrome"
