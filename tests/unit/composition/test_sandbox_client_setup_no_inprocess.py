"""The main service renders DOCX and PDF only through the sandbox: an empty URL degrades everywhere, a stale
SANDBOX_RENDER value changes nothing (UNTRUSTED_CONTENT_SANDBOX_RFC §4.12)."""
import pytest

from src.adapters.sandbox_client.sandbox_docx_runner import SandboxDocxRunner
from src.adapters.sandbox_client.sandbox_puppeteer_runner import SandboxPuppeteerRunner
from src.composition import sandbox_client_setup as setup
from src.domain.sandbox_errors import SandboxUnavailable
from src.ports.puppeteer_runner_port import PuppeteerRunnerError


def _env(**values):
    return lambda key, default=None: values.get(key, default)


@pytest.fixture
def errors(monkeypatch):
    calls = []
    monkeypatch.setattr(setup.logger, "error", lambda msg, *a, **k: calls.append(msg % a if a else msg))
    return calls


@pytest.mark.parametrize("on_cloud_run", [False, True])
async def test_empty_url_degrades_docx_without_raising(errors, on_cloud_run):
    runner = setup.build_docx_runner(_env(), on_cloud_run=on_cloud_run)
    assert isinstance(runner, setup._UnavailableDocxRunner)
    assert len(errors) == 1 and "SANDBOX_RENDER_URL" in errors[0]
    with pytest.raises(SandboxUnavailable):
        await runner.run("x", "{}", 5)


@pytest.mark.parametrize("on_cloud_run", [False, True])
async def test_empty_url_degrades_pdf_without_raising(errors, on_cloud_run):
    runner = setup.build_pdf_runner(_env(), on_cloud_run=on_cloud_run)
    assert isinstance(runner, setup._UnavailablePuppeteerRunner)
    assert len(errors) == 1 and "SANDBOX_RENDER_URL" in errors[0]
    with pytest.raises(PuppeteerRunnerError):
        await runner.run("<html></html>", 5)


def test_factories_build_once_and_share_and_log_once(errors):
    docx, pdf = setup.docx_runner_factory(_env(), False), setup.pdf_runner_factory(_env(), False)
    assert docx() is docx() and pdf() is pdf()
    assert len(errors) == 2  # one per feature, at build time


def test_a_set_url_builds_the_sandbox_runners():
    env = _env(SANDBOX_RENDER_URL="https://render.example.test")
    assert isinstance(setup.build_docx_runner(env, False), SandboxDocxRunner)
    assert isinstance(setup.build_pdf_runner(env, False), SandboxPuppeteerRunner)
    factory = setup.docx_runner_factory(env, False)
    assert factory() is factory()


@pytest.mark.parametrize("value", ["inprocess", "sandbox", "garbage", ""])
def test_a_stale_sandbox_render_value_is_ignored(value):
    env = _env(SANDBOX_RENDER=value, SANDBOX_RENDER_URL="https://render.example.test")
    assert isinstance(setup.build_docx_runner(env, False), SandboxDocxRunner)
    assert isinstance(setup.build_pdf_runner(env, False), SandboxPuppeteerRunner)
    degraded = _env(SANDBOX_RENDER=value)
    assert isinstance(setup.build_docx_runner(degraded, False), setup._UnavailableDocxRunner)
