"""Adapted from the first T8b version: the switch is now resolved once in `UserAgentFactory.__init__`
(`self._docx_runner_factory`), so the seam under test moved from a per-build call to that attribute."""
import logging
from unittest.mock import MagicMock

import pytest

from src.adapters.node_docx_runner import NodeDocxRunner
from src.composition import user_agent_factory as uaf
from src.composition.sandbox_client_setup import docx_runner_factory
from src.composition.user_agent_factory import UserAgentFactory


def _env(**kw):
    return lambda name, default=None: kw.get(name, default)


def test_factory_wires_the_runner_from_the_switch():
    sentinel = object()
    fake_self = MagicMock()
    fake_self._docx_runner_factory = lambda: sentinel
    agent = UserAgentFactory._build_doc_generator(fake_self, "user1", MagicMock())
    assert agent._runner is sentinel


def _construct():
    import inspect
    params = inspect.signature(UserAgentFactory.__init__).parameters
    kwargs = {n: MagicMock() for n, p in params.items()
              if n != "self" and p.default is inspect.Parameter.empty}
    return UserAgentFactory(**kwargs)


def test_constructing_the_factory_with_a_bad_value_raises(monkeypatch):
    monkeypatch.setenv("SANDBOX_RENDER", "maybe")
    with pytest.raises(ValueError):
        _construct()


def test_constructing_the_factory_unset_keeps_the_node_runner(monkeypatch):
    monkeypatch.delenv("SANDBOX_RENDER", raising=False)
    assert isinstance(_construct()._docx_runner_factory(), NodeDocxRunner)


def test_inprocess_builds_a_runner_per_build():
    make = docx_runner_factory(getenv=_env(), on_cloud_run=True)
    assert isinstance(make(), NodeDocxRunner) and make() is not make()


def test_sandbox_mode_shares_one_runner_across_users():
    make = docx_runner_factory(
        getenv=_env(SANDBOX_RENDER="sandbox", SANDBOX_RENDER_URL="https://sandbox.example"), on_cloud_run=True)
    fake_self = MagicMock()
    fake_self._docx_runner_factory = make
    a = UserAgentFactory._build_doc_generator(fake_self, "u1", MagicMock())
    b = UserAgentFactory._build_doc_generator(fake_self, "u2", MagicMock())
    assert a._runner is b._runner


def test_degraded_error_is_logged_once_not_per_user(caplog):
    with caplog.at_level(logging.ERROR):
        make = docx_runner_factory(getenv=_env(SANDBOX_RENDER="sandbox"), on_cloud_run=True)
        make(), make(), make()
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
