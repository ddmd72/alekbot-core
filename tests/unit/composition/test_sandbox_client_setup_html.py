"""One knob (SANDBOX_RENDER / SANDBOX_RENDER_URL) for the PDF runner and the widget PNG renderer."""
import logging

import pytest

from src.adapters.node_puppeteer_runner import NodePuppeteerRunner
from src.adapters.sandbox_client.sandbox_html_renderer import SandboxHtmlRenderer
from src.adapters.sandbox_client.sandbox_puppeteer_runner import SandboxPuppeteerRunner
from src.composition.sandbox_client_setup import build_html_renderer, build_pdf_runner, pdf_runner_factory
from src.ports.html_renderer_port import HtmlRenderError
from src.ports.puppeteer_runner_port import PuppeteerRunnerError

URL = "https://sandbox.example/"


def _env(**kw):
    return lambda name, default=None: kw.get(name, default)


class _Marker:
    pass


def test_inprocess_default_builds_node_runner_and_factory_object():
    assert isinstance(build_pdf_runner(getenv=_env(), on_cloud_run=True), NodePuppeteerRunner)
    marker = _Marker()
    assert build_html_renderer(True, lambda: marker, getenv=_env(), on_cloud_run=True) is marker


def test_sandbox_with_url_builds_sandbox_classes():
    env = _env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL=URL)
    assert isinstance(build_pdf_runner(getenv=env, on_cloud_run=True), SandboxPuppeteerRunner)
    assert isinstance(build_html_renderer(True, _Marker, getenv=env, on_cloud_run=True), SandboxHtmlRenderer)


def test_pdf_factory_shares_one_instance_in_sandbox_mode():
    factory = pdf_runner_factory(getenv=_env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL=URL), on_cloud_run=True)
    assert factory() is factory()
    assert isinstance(factory(), SandboxPuppeteerRunner)


def test_pdf_factory_inprocess_builds_node_runner_per_call():
    factory = pdf_runner_factory(getenv=_env(), on_cloud_run=True)
    assert isinstance(factory(), NodePuppeteerRunner)


async def test_sandbox_empty_url_on_cloud_run_degrades_with_error_log(caplog):
    env = _env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL=" ")
    with caplog.at_level(logging.ERROR):
        pdf = build_pdf_runner(getenv=env, on_cloud_run=True)
        png = build_html_renderer(True, _Marker, getenv=env, on_cloud_run=True)
    assert sum(1 for r in caplog.records if r.levelno >= logging.ERROR) == 2
    with pytest.raises(PuppeteerRunnerError):
        await pdf.run("<p>x</p>", 5)
    with pytest.raises(HtmlRenderError):
        await png.render("<p>x</p>")


def test_sandbox_empty_url_locally_is_startup_error():
    env = _env(SANDBOX_RENDER="sandbox")
    with pytest.raises(ValueError):
        build_pdf_runner(getenv=env, on_cloud_run=False)
    with pytest.raises(ValueError):
        build_html_renderer(True, _Marker, getenv=env, on_cloud_run=False)


def test_bad_value_is_startup_error():
    env = _env(SANDBOX_RENDER="maybe")
    with pytest.raises(ValueError):
        build_pdf_runner(getenv=env, on_cloud_run=True)
    with pytest.raises(ValueError):
        pdf_runner_factory(getenv=env, on_cloud_run=True)
    with pytest.raises(ValueError):
        build_html_renderer(True, _Marker, getenv=env, on_cloud_run=True)


@pytest.mark.parametrize("mode", ["inprocess", "sandbox"])
def test_disabled_renderer_is_none_in_every_mode(mode):
    env = _env(SANDBOX_RENDER=mode, SANDBOX_RENDER_URL=URL)
    assert build_html_renderer(False, _Marker, getenv=env, on_cloud_run=True) is None
