import inspect
import logging

import pytest

from src.adapters.node_docx_runner import NodeDocxRunner
from src.adapters.sandbox_client.sandbox_docx_runner import SandboxDocxRunner
from src.composition.sandbox_client_setup import build_docx_runner
from src.domain.sandbox_errors import SandboxUnavailable


def _env(**kw):
    return lambda name, default=None: kw.get(name, default)


def test_inprocess_is_default_and_builds_node_runner():
    assert isinstance(build_docx_runner(getenv=_env(), on_cloud_run=True), NodeDocxRunner)
    assert isinstance(build_docx_runner(getenv=_env(SANDBOX_RENDER="inprocess"), on_cloud_run=False), NodeDocxRunner)


def test_sandbox_mode_builds_sandbox_runner():
    runner = build_docx_runner(
        getenv=_env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL="https://sandbox.example/"), on_cloud_run=True)
    assert isinstance(runner, SandboxDocxRunner)


def test_unknown_value_is_startup_error():
    with pytest.raises(ValueError):
        build_docx_runner(getenv=_env(SANDBOX_RENDER="maybe"), on_cloud_run=True)


async def test_sandbox_without_url_on_cloud_run_degrades_and_logs_error_and_runner_raises_unavailable(caplog):
    with caplog.at_level(logging.ERROR):
        runner = build_docx_runner(getenv=_env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL=" "), on_cloud_run=True)
    assert not isinstance(runner, (NodeDocxRunner, SandboxDocxRunner))
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    with pytest.raises(SandboxUnavailable):
        await runner.run("x", "{}", 5)


def test_sandbox_without_url_locally_is_startup_error():
    with pytest.raises(ValueError):
        build_docx_runner(getenv=_env(SANDBOX_RENDER="sandbox"), on_cloud_run=False)


def test_knob_read_with_getenv_not_load_settings():
    from src.config import settings
    src = inspect.getsource(settings)
    assert "SANDBOX_RENDER" not in src
