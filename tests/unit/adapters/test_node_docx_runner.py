"""
Wire tests for NodeDocxRunner — security-focused.

Mock boundary: asyncio.create_subprocess_exec (subprocess layer).
Never mock at DocxRunnerPort level — that hides translation bugs.

The Node script run here is LLM-generated and therefore untrusted. These tests
lock in the two sandboxing guarantees added for the public-release hardening:
  1. The subprocess inherits NO application secrets (allow-listed env only).
  2. A security prelude is prepended to the script, before any user code, that
     blocks network/process-spawning core modules and global fetch.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.node_docx_runner import (
    NodeDocxRunner,
    _SECURITY_PRELUDE,
    _safe_subprocess_env,
)
from src.ports.docx_runner_port import DocxRunnerError


_FAKE_DOCX = b"PK\x03\x04 fake-docx-bytes"
_SPEC = '{"title": "x"}'


@pytest.fixture
def runner():
    return NodeDocxRunner()


def _make_proc(returncode=0, stdout=_FAKE_DOCX, stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestNodeDocxRunnerSandboxEnv:
    """The subprocess env must carry only allow-listed keys, never secrets."""

    def test_safe_env_excludes_secrets_and_keeps_path(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-should-not-leak")
        monkeypatch.setenv("OAUTH_SESSION_SECRET", "super-secret-signing-key")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/run/secrets/sa.json")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-leak")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        env = _safe_subprocess_env()

        assert env.get("PATH") == "/usr/bin:/bin"
        assert "ANTHROPIC_API_KEY" not in env
        assert "OAUTH_SESSION_SECRET" not in env
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
        assert "GEMINI_API_KEY" not in env

    def test_safe_env_omits_unset_keys(self, monkeypatch):
        # Allow-listed keys that are unset must not appear as empty strings.
        for k in ("HOME", "NODE_PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PATH", "/usr/bin")
        env = _safe_subprocess_env()
        assert env == {"PATH": "/usr/bin"}

    @pytest.mark.asyncio
    async def test_run_passes_safe_env_to_subprocess(self, runner, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-should-not-leak")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        proc = _make_proc()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.name = "/tmp/fake.js"
                with patch("os.unlink"):
                    await runner.run("console.log('x')", _SPEC, timeout=30)

        env = mock_exec.call_args.kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin:/bin"


class TestNodeDocxRunnerSecurityPrelude:
    """The prelude must be prepended before user code and block egress vectors."""

    def test_prelude_blocks_network_and_process_modules(self):
        for mod in ("child_process", "http", "https", "net", "dns", "tls"):
            assert f"'{mod}'" in _SECURITY_PRELUDE
        assert "globalThis.fetch" in _SECURITY_PRELUDE
        # docx toolchain modules must NOT be blocked.
        for allowed in ("fs", "path", "zlib", "stream", "buffer"):
            assert f"'{allowed}'" not in _SECURITY_PRELUDE

    @pytest.mark.asyncio
    async def test_prelude_prepended_before_user_code(self, runner):
        chunks = []
        proc = _make_proc()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.name = "/tmp/fake.js"
                mock_tmp.return_value.write.side_effect = lambda d: chunks.append(d)
                with patch("os.unlink"):
                    await runner.run("console.log('USER_CODE_MARKER')", _SPEC, timeout=30)

        content = "".join(chunks)
        assert "Blocked module for security" in content
        assert "USER_CODE_MARKER" in content
        assert content.index("Blocked module") < content.index("USER_CODE_MARKER")


class TestNodeDocxRunnerBehaviour:
    """Baseline runner contract still holds after the hardening."""

    @pytest.mark.asyncio
    async def test_success_returns_docx_bytes(self, runner):
        proc = _make_proc(stdout=_FAKE_DOCX)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.name = "/tmp/fake.js"
                with patch("os.unlink"):
                    result = await runner.run("x", _SPEC, timeout=30)
        assert result == _FAKE_DOCX

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self, runner):
        proc = _make_proc(returncode=1, stderr=b"boom")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.name = "/tmp/fake.js"
                with patch("os.unlink"):
                    with pytest.raises(DocxRunnerError, match="exit code 1"):
                        await runner.run("x", _SPEC, timeout=30)

    @pytest.mark.asyncio
    async def test_empty_stdout_raises(self, runner):
        proc = _make_proc(returncode=0, stdout=b"")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.name = "/tmp/fake.js"
                with patch("os.unlink"):
                    with pytest.raises(DocxRunnerError, match="empty"):
                        await runner.run("x", _SPEC, timeout=30)

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, runner):
        proc = _make_proc()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError())):
                with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                    mock_tmp.return_value.name = "/tmp/fake.js"
                    with patch("os.unlink"):
                        with pytest.raises(DocxRunnerError, match="timed out"):
                            await runner.run("x", _SPEC, timeout=30)
        proc.kill.assert_called_once()
