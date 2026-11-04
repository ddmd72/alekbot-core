"""Owner decision O16: the widget PNG renderer is always the in-process one, whatever SANDBOX_RENDER says."""
import pytest

from src.composition.sandbox_client_setup import build_html_renderer

URL = "https://sandbox.example/"


def _env(**kw):
    return lambda name, default=None: kw.get(name, default)


class _Marker:
    pass


@pytest.mark.parametrize("env", [
    _env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL=URL),
    _env(SANDBOX_RENDER="sandbox"),
    _env(),
])
@pytest.mark.parametrize("cloud", [True, False])
def test_enabled_renderer_is_the_inprocess_one(env, cloud):
    marker = _Marker()
    assert build_html_renderer(True, lambda: marker, getenv=env, on_cloud_run=cloud) is marker


def test_sandbox_mode_without_arguments_still_builds_inprocess(monkeypatch):
    monkeypatch.setenv("SANDBOX_RENDER", "sandbox")
    monkeypatch.setenv("SANDBOX_RENDER_URL", URL)
    marker = _Marker()
    assert build_html_renderer(True, lambda: marker) is marker


@pytest.mark.parametrize("mode", ["inprocess", "sandbox"])
def test_disabled_renderer_is_none(mode):
    env = _env(SANDBOX_RENDER=mode, SANDBOX_RENDER_URL=URL)
    assert build_html_renderer(False, _Marker, getenv=env, on_cloud_run=True) is None
