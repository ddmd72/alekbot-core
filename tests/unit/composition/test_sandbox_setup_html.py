"""Render family registry: render.pdf / render.png over the browser launcher (S2 card C)."""
import os

from src.adapters.sandbox_engine.docx_render_engine import DOCX_MEMORY_MODE, DocxRenderEngine
from src.adapters.sandbox_engine.html_render_engine import (
    BROWSER_MEMORY_MODE, BROWSER_NOFILE, BROWSER_NPROC, HtmlRenderEngine,
)
from src.composition.sandbox_setup import build_engine_registry, validate_startup
from src.domain.sandbox_contract import SandboxOp


def _plan(extra=None, family="render"):
    env = {"SANDBOX_SERVICE": family, "SANDBOX_OUTPUT_ROOT": "", **(extra or {})}
    key = "SANDBOX_CALLERS_RENDER" if family == "render" else "SANDBOX_CALLERS_MAIN"
    env[key] = "caller@example.com"
    env.pop("SANDBOX_OUTPUT_ROOT")
    return validate_startup(env, on_cloud_run=False)


def _registry(tmp_path, extra=None, family="render"):
    plan = _plan({"SANDBOX_OUTPUT_ROOT": str(tmp_path / "out"), "SANDBOX_WORK_ROOT": str(tmp_path / "work"),
                  **(extra or {})}, family)
    return plan, build_engine_registry(plan)


def test_render_registry_maps_pdf_and_png_to_html_engine(tmp_path):
    _, registry = _registry(tmp_path)
    assert set(registry) == {SandboxOp.RENDER_DOCX, SandboxOp.RENDER_PDF, SandboxOp.RENDER_PNG}
    assert isinstance(registry[SandboxOp.RENDER_PDF], HtmlRenderEngine)
    assert isinstance(registry[SandboxOp.RENDER_PNG], HtmlRenderEngine)
    assert isinstance(registry[SandboxOp.RENDER_DOCX], DocxRenderEngine)


def test_browser_engines_share_a_launcher_with_browser_limits(tmp_path):
    _, registry = _registry(tmp_path)
    pdf, png = registry[SandboxOp.RENDER_PDF], registry[SandboxOp.RENDER_PNG]
    launcher = pdf._launcher
    assert png._launcher is launcher
    assert launcher._memory_mode == BROWSER_MEMORY_MODE == "data"
    assert launcher._nproc == BROWSER_NPROC == 512
    assert launcher._nofile == BROWSER_NOFILE == 1024


def test_docx_engine_keeps_its_own_launcher_and_limits(tmp_path):
    _, registry = _registry(tmp_path)
    docx_launcher = registry[SandboxOp.RENDER_DOCX]._launcher
    assert docx_launcher is not registry[SandboxOp.RENDER_PDF]._launcher
    assert docx_launcher._memory_mode == DOCX_MEMORY_MODE == "data"
    assert docx_launcher._nproc != BROWSER_NPROC
    assert docx_launcher._nofile != BROWSER_NOFILE


def test_light_registry_has_no_render_op(tmp_path):
    _, registry = _registry(tmp_path, family="light")
    assert not any(op.value.startswith("render.") for op in registry)


def test_browser_cache_dir_from_env_and_default(tmp_path):
    plan, registry = _registry(tmp_path, {"PUPPETEER_CACHE_DIR": "/opt/puppeteer"})
    assert plan.browser_cache_dir == "/opt/puppeteer"
    assert registry[SandboxOp.RENDER_PDF]._browser_cache_dir == "/opt/puppeteer"
    default_plan, default_registry = _registry(tmp_path)
    assert default_plan.browser_cache_dir
    assert default_registry[SandboxOp.RENDER_PNG]._browser_cache_dir == os.path.expanduser(
        os.path.join("~", ".cache", "puppeteer"))
