"""HtmlRenderEngine with the real child, node, Puppeteer and chrome-headless-shell (slice S2): a PDF of the household budget
and PNGs of the Rome trip card at two widths (IHDR width = 2 x viewport width: device scale factor 2).

Skipped unless node, `pdf_generator/node_modules/puppeteer` and a chrome-headless-shell in the Puppeteer cache are present,
and as root (the child must not run as root). Limits on macOS are not Linux's: NPROC counts every process of the user
there, so the local launcher lifts it; RLIMIT_DATA is tolerated as unsupported (limits_exec)."""
import os
import shutil
import struct
import sys

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

_BUDGET = (
    "<!doctype html><html><head><meta charset='utf-8'><style>@page{size:A4;margin:20mm}"
    "body{font-family:sans-serif}td{padding:4px 12px}</style></head><body><h1>Household budget 2026</h1>"
    "<table><tr><td>Rent</td><td>11,400</td></tr><tr><td>Groceries</td><td>4,200</td></tr>"
    "<tr><td>Transport</td><td>960</td></tr><tr><td>Holidays</td><td>2,500</td></tr></table></body></html>"
)
_ROME_CARD = (
    "<div style=\"font-family:sans-serif;padding:16px;border-radius:12px;background:#f4efe6\">"
    "<h3>Albergo Immaginario</h3><p>Booking RM-55120</p><p>10–14 Aug 2025</p><p><b>560 EUR</b></p></div>"
)


def _limits():
    return SandboxLimits(wall_s=60.0, memory_bytes=1 << 30, cpu_s=60.0, file_bytes=64 * 1024 * 1024)


def _launcher():
    nproc = 4000 if sys.platform == "darwin" else BROWSER_NPROC
    return ChildLauncher(memory_mode=BROWSER_MEMORY_MODE, nproc=nproc, nofile=BROWSER_NOFILE)


async def _render(tmp_path, op, body: bytes):
    workdir = tmp_path / "work"
    workdir.mkdir()
    out_root = tmp_path / "out"
    out_root.mkdir(mode=0o700)
    body_path = tmp_path / "body"
    body_path.write_bytes(body)
    engine = HtmlRenderEngine(_launcher(), op=op, output_root=str(out_root), browser_cache_dir=_CACHE_DIR)
    limits = _limits()
    result = await engine.run(str(body_path), str(workdir), limits, SandboxMeta(request_id="r" * 16, limits=limits))
    assert result.status is SandboxStatus.OK, result.detail
    with open(result.output_path, "rb") as fh:
        return fh.read()


async def test_budget_pdf(tmp_path):
    data = await _render(tmp_path, SandboxOp.RENDER_PDF, _BUDGET.encode())
    assert data.startswith(b"%PDF-")


@pytest.mark.parametrize("width", [480, 320])
async def test_rome_card_png_width(tmp_path, width):
    data = await _render(tmp_path, SandboxOp.RENDER_PNG, frame_png_body(width, _ROME_CARD.encode()))
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    ihdr_width, _ = struct.unpack(">II", data[16:24])
    assert ihdr_width == 2 * width
