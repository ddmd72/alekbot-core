"""A render makes zero network connections (card F; RFC §12): the real engine with a 127.0.0.1 listener.

The page references the listener (and the cloud metadata hosts) through <img>, <link rel=stylesheet>, CSS @import,
a url() font, <iframe> and <object>. The render must still end `ok`, and the listener must accept nothing.
"""
import os
import shutil
import socket
import sys
import threading

import pytest

from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.html_render_engine import (
    BROWSER_MEMORY_MODE, BROWSER_NOFILE, BROWSER_NPROC, HtmlRenderEngine,
)
from src.domain.sandbox_contract import SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus
from src.domain.sandbox_png_frame import frame_png_body

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_CACHE_DIR = os.environ.get("PUPPETEER_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "puppeteer")


def _has_headless_shell() -> bool:
    shell_dir = os.path.join(_CACHE_DIR, "chrome-headless-shell")
    return os.path.isdir(shell_dir) and bool(os.listdir(shell_dir))


_READY = (shutil.which("node") is not None
          and os.path.isdir(os.path.join(_ROOT_DIR, "pdf_generator", "node_modules", "puppeteer"))
          and _has_headless_shell())

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _READY, reason="needs node, pdf_generator/node_modules/puppeteer and chrome-headless-shell"),
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"), reason="needs a non-root user"),
]


class _Listener:
    """Counts the connections a 127.0.0.1 ephemeral-port socket accepts."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._sock.close()
        self._thread.join(timeout=2)


@pytest.fixture
def listener():
    srv = _Listener()
    yield srv
    srv.close()


def _page(port: int) -> str:
    local = f"http://127.0.0.1:{port}"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<link rel='stylesheet' href='{local}/a.css'>"
        "<link rel='stylesheet' href='http://metadata.google.internal/computeMetadata/v1/b.css'>"
        f"<style>@import url('{local}/imported.css');"
        f"@font-face{{font-family:Remote;src:url('{local}/f.woff2') format('woff2')}}"
        "body{font-family:Remote,sans-serif}"
        f".bg{{background:url('http://169.254.169.254/computeMetadata/v1/bg.png')}}</style></head><body>"
        "<h1>Household budget 2026</h1><div class='bg'>Example Mutual Insurance, ends in 8204</div>"
        f"<img src='{local}/logo.png'><img src='http://169.254.169.254/latest/meta-data/x.png'>"
        f"<iframe src='{local}/frame.html'></iframe><object data='{local}/o.pdf'></object>"
        "<img src='https://example.com/logo.png'></body></html>"
    )


async def _render(tmp_path, op, body: bytes):
    workdir = tmp_path / "work"
    workdir.mkdir()
    out_root = tmp_path / "out"
    out_root.mkdir(mode=0o700)
    body_path = tmp_path / "body"
    body_path.write_bytes(body)
    nproc = 4000 if sys.platform == "darwin" else BROWSER_NPROC
    engine = HtmlRenderEngine(ChildLauncher(memory_mode=BROWSER_MEMORY_MODE, nproc=nproc, nofile=BROWSER_NOFILE),
                              op=op, output_root=str(out_root), browser_cache_dir=_CACHE_DIR)
    limits = SandboxLimits(wall_s=60.0, memory_bytes=1 << 30, cpu_s=60.0, file_bytes=64 * 1024 * 1024)
    return await engine.run(str(body_path), str(workdir), limits, SandboxMeta(request_id="n" * 16, limits=limits))


async def test_pdf_render_makes_no_connection(tmp_path, listener):
    result = await _render(tmp_path, SandboxOp.RENDER_PDF, _page(listener.port).encode())
    assert result.status is SandboxStatus.OK, result.detail
    assert listener.accepted == 0


async def test_png_render_makes_no_connection(tmp_path, listener):
    result = await _render(tmp_path, SandboxOp.RENDER_PNG, frame_png_body(480, _page(listener.port).encode()))
    assert result.status is SandboxStatus.OK, result.detail
    assert listener.accepted == 0
