"""Owner decision O16: the widget PNG renderer is always the in-process one, whatever SANDBOX_RENDER says."""
import pytest

from src.composition.sandbox_client_setup import build_html_renderer

URL = "https://sandbox.example/"


class _Marker:
    pass


@pytest.mark.parametrize("env", [
    {"SANDBOX_RENDER": "sandbox", "SANDBOX_RENDER_URL": URL},
    {"SANDBOX_RENDER": "sandbox"},
    {},
])
def test_enabled_renderer_is_the_inprocess_one_whatever_the_knob_says(env, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    marker = _Marker()
    assert build_html_renderer(True, lambda: marker) is marker


def test_disabled_renderer_is_none(monkeypatch):
    monkeypatch.setenv("SANDBOX_RENDER", "sandbox")
    monkeypatch.setenv("SANDBOX_RENDER_URL", URL)
    assert build_html_renderer(False, _Marker) is None
