from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agents.tutor_agent import TutorAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext


def _make_ctx() -> _UserContext:
    user_profile = MagicMock()
    user_profile.config.timezone = "Europe/Madrid"
    return _UserContext(user_profile=user_profile, prompt_builder=MagicMock())


def _make_factory_self(assembler=None) -> SimpleNamespace:
    """A minimal object carrying only what _build_tutor reads from self."""
    context_builder = MagicMock()
    context_builder.build.return_value = MagicMock(provider=MagicMock(), model_name="claude-sonnet-5")
    return SimpleNamespace(
        context_builder=context_builder,
        companion_context_assembler=assembler,
    )


def test_build_tutor_returns_none_when_no_assembler_configured():
    """No companion_context_assembler configured → degrade gracefully, don't crash."""
    fake_self = _make_factory_self(assembler=None)
    agent = UserAgentFactory._build_tutor(fake_self, "user-1", _make_ctx())
    assert agent is None


def test_build_tutor_wires_assembler_into_agent():
    assembler = MagicMock()
    fake_self = _make_factory_self(assembler=assembler)
    agent = UserAgentFactory._build_tutor(fake_self, "user-1", _make_ctx())
    assert isinstance(agent, TutorAgent)
    assert agent._assembler is assembler


def test_build_tutor_agent_id_includes_user_id():
    fake_self = _make_factory_self(assembler=MagicMock())
    agent = UserAgentFactory._build_tutor(fake_self, "user-1", _make_ctx())
    assert agent.agent_id == "tutor_agent_user-1"


def test_lazy_dispatch_tables_include_tutor():
    assert "tutor" in UserAgentFactory._LAZY_BUILDERS
    assert UserAgentFactory._LAZY_AGENT_IDS["tutor"] == "tutor_agent"
