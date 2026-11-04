"""NodePuppeteerRunner vs SandboxPuppeteerRunner: equal observable outcomes (card F; RFC §4.7).

Sandbox variant: client -> real sandbox_app (ASGI) -> real HtmlRenderEngine -> real node + headless shell.
Skipped without node, pdf_generator/node_modules/puppeteer, a chrome-headless-shell, or as root.
"""
import os
import shutil
import sys

import pytest

from src.adapters.node_puppeteer_runner import NodePuppeteerRunner
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.adapters.sandbox_client.sandbox_puppeteer_runner import SandboxPuppeteerRunner
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.html_render_engine import (
    BROWSER_MEMORY_MODE, BROWSER_NOFILE, BROWSER_NPROC, HtmlRenderEngine,
)
from src.domain.sandbox_contract import SandboxOp
from src.ports.puppeteer_runner_port import PuppeteerRunnerError
from tests.unit.adapters.sandbox_client.render_html_helpers import HOUSEHOLD_BUDGET_HTML, render_app_transport
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, good_token

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_CACHE_DIR = os.environ.get("PUPPETEER_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "puppeteer")


def _has_headless_shell() -> bool:
    shell_dir = os.path.join(_CACHE_DIR, "chrome-headless-shell")
    return os.path.isdir(shell_dir) and bool(os.listdir(shell_dir))


_READY = (shutil.which("node") is not None
          and os.path.isdir(os.path.join(_ROOT, "pdf_generator", "node_modules", "puppeteer"))
          and _has_headless_shell())

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _READY, reason="needs node, pdf_generator/node_modules/puppeteer and chrome-headless-shell"),
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"), reason="needs a non-root user"),
]

_BIG_TABLE = ("<html><body><h1>Household budget 2026</h1><table>"
              + "".join(f"<tr><td>line {i}</td><td>{i * 7 % 1000},00</td></tr>" for i in range(20000))
              + "</table></body></html>")


async def _no_sleep(_s):
    return None


@pytest.fixture(params=["node", "sandbox"])
def runner(request, tmp_path):
    if request.param == "node":
        return NodePuppeteerRunner()
    nproc = 4000 if sys.platform == "darwin" else BROWSER_NPROC
    launcher = ChildLauncher(memory_mode=BROWSER_MEMORY_MODE, nproc=nproc, nofile=BROWSER_NOFILE)
    out_root = tmp_path / "engine-out"
    out_root.mkdir(mode=0o700)
    engine = HtmlRenderEngine(launcher, op=SandboxOp.RENDER_PDF, output_root=str(out_root),
                              browser_cache_dir=_CACHE_DIR)
    transport = render_app_transport(tmp_path, pdf_engine=engine)
    return SandboxPuppeteerRunner(SandboxHttpClient(BASE, good_token, transport=transport, sleep=_no_sleep))


async def test_household_budget_renders_a_pdf(runner):
    assert (await runner.run(HOUSEHOLD_BUDGET_HTML, 60)).startswith(b"%PDF-")


async def test_a_20000_row_table_hits_the_one_second_timeout(runner):
    with pytest.raises(PuppeteerRunnerError) as info:
        await runner.run(_BIG_TABLE, 1)
    assert "timed out" in str(info.value)


async def test_empty_html_is_a_runner_error(runner):
    with pytest.raises(PuppeteerRunnerError):
        await runner.run("", 60)
