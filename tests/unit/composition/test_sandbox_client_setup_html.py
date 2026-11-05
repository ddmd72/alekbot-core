"""SANDBOX_RENDER_URL selects the PDF sandbox (a stale SANDBOX_RENDER is ignored); the widget PNG renderer is off it (O16)."""
import logging

import pytest

from src.adapters.sandbox_client.sandbox_puppeteer_runner import SandboxPuppeteerRunner
from src.composition.sandbox_client_setup import build_html_renderer, build_pdf_runner, pdf_runner_factory
from src.ports.puppeteer_runner_port import PuppeteerRunnerError

URL = "https://sandbox.example/"


def _env(**kw):
    return lambda name, default=None: kw.get(name, default)


class _Marker:
    pass


def test_html_renderer_is_the_inprocess_factory_object():
    marker = _Marker()
    assert build_html_renderer(True, lambda: marker) is marker


def test_sandbox_with_url_builds_sandbox_pdf_runner():
    env = _env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL=URL)
    assert isinstance(build_pdf_runner(getenv=env), SandboxPuppeteerRunner)


def test_pdf_factory_shares_one_instance_in_sandbox_mode():
    factory = pdf_runner_factory(getenv=_env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL=URL))
    assert factory() is factory()
    assert isinstance(factory(), SandboxPuppeteerRunner)


async def test_sandbox_empty_url_on_cloud_run_degrades_with_error_log(caplog):
    env = _env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL=" ")
    with caplog.at_level(logging.ERROR):
        pdf = build_pdf_runner(getenv=env)
    assert sum(1 for r in caplog.records if r.levelno >= logging.ERROR) == 1
    with pytest.raises(PuppeteerRunnerError):
        await pdf.run("<p>x</p>", 5)


@pytest.mark.parametrize("mode", ["inprocess", "sandbox"])
def test_disabled_renderer_is_none_in_every_mode(mode, monkeypatch):
    monkeypatch.setenv("SANDBOX_RENDER", mode)
    monkeypatch.setenv("SANDBOX_RENDER_URL", URL)
    assert build_html_renderer(False, _Marker) is None
