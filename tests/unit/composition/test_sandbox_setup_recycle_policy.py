"""SANDBOX_RECYCLE: the render service keeps `always`; `abnormal` is for the light service only."""
import pytest

from src.composition.sandbox_setup import SandboxStartupError, validate_startup

LIGHT = {"SANDBOX_SERVICE": "light", "SANDBOX_CALLERS_MAIN": "main@example.com"}
RENDER = {"SANDBOX_CALLERS_RENDER": "r@example.com"}


def test_render_default_policy_is_always():
    assert validate_startup(dict(RENDER), on_cloud_run=False).recycle == "always"


def test_light_accepts_abnormal_on_cloud_run():
    plan = validate_startup({**LIGHT, "SANDBOX_RECYCLE": "abnormal", "K_SERVICE": "svc"}, on_cloud_run=True)
    assert plan.recycle == "abnormal" and plan.family == "light"


def test_render_refuses_abnormal():
    with pytest.raises(SandboxStartupError, match="light"):
        validate_startup({**RENDER, "SANDBOX_RECYCLE": "abnormal"}, on_cloud_run=False)
