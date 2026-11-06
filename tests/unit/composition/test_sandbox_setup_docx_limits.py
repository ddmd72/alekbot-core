"""Render family registry: the render.docx engine's launcher limits (G6 / S0-f; prod abort 2026-11-06)."""
from src.adapters.sandbox_engine.docx_render_engine import DOCX_MEMORY_MODE
from src.adapters.sandbox_engine.limits_exec import NOFILE_DEFAULT, NPROC_DEFAULT
from src.composition.sandbox_setup import build_engine_registry, validate_startup
from src.domain.sandbox_contract import SandboxOp


def _registry(tmp_path):
    plan = validate_startup({"SANDBOX_SERVICE": "render", "SANDBOX_CALLERS_RENDER": "caller@example.com",
                             "SANDBOX_OUTPUT_ROOT": str(tmp_path / "out"), "SANDBOX_WORK_ROOT": str(tmp_path / "work")},
                            on_cloud_run=False)
    return build_engine_registry(plan)


def test_docx_launcher_bounds_writable_memory_not_address_space(tmp_path):
    launcher = _registry(tmp_path)[SandboxOp.RENDER_DOCX]._launcher
    assert launcher._memory_mode == DOCX_MEMORY_MODE == "data"


def test_docx_launcher_keeps_the_default_process_and_file_limits(tmp_path):
    registry = _registry(tmp_path)
    launcher = registry[SandboxOp.RENDER_DOCX]._launcher
    assert launcher is not registry[SandboxOp.RENDER_PDF]._launcher
    assert (launcher._nproc, launcher._nofile) == (NPROC_DEFAULT, NOFILE_DEFAULT) == (64, 64)
