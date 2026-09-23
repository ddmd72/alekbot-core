from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.user import UserBotConfig


def _ctx():
    profile = MagicMock()
    profile.config = UserBotConfig(timezone="Europe/Madrid")
    return _UserContext(user_profile=profile, prompt_builder=MagicMock())


# Short digit count deliberately: the repo's pre-commit hook flags "+" followed by
# 7-15 digits as a possible real phone number; this value is never asserted on below,
# only checked for truthiness, so it stays under that threshold like other test fixtures
# in this repo (e.g. tests/unit/web/test_voice_webhook_app.py's "+346001").
def _self(telephony=True, phone="+346001", notifications=True):
    return SimpleNamespace(
        _telephony=MagicMock() if telephony else None,
        config={"TWILIO_PHONE_NUMBER": phone, "CLOUD_RUN_SERVICE_URL": "https://main.example.com"},
        notification_service=MagicMock() if notifications else None,
        repository=MagicMock(),
        session_store=MagicMock(),
    )


def test_build_lelik_wires_the_users_prompt_builder_and_persona():
    ctx = _ctx()
    fake = _self()
    agent = UserAgentFactory._build_lelik(fake, "u1", ctx)
    assert isinstance(agent, LelikAgent)
    assert agent.agent_id == "lelik_agent_u1"
    assert agent._prompt_builder is ctx.prompt_builder
    assert agent._persona._notifications is fake.notification_service
    assert agent._persona._config is ctx.user_profile.config
    assert agent._status_callback_url == "https://main.example.com/voice/status"


@pytest.mark.parametrize("kwargs", [{"telephony": False}, {"phone": None}, {"notifications": False}])
def test_build_lelik_skips_when_a_dependency_is_missing(kwargs):
    assert UserAgentFactory._build_lelik(_self(**kwargs), "u1", _ctx()) is None


def test_lazy_tables_include_lelik():
    assert "lelik" in UserAgentFactory._LAZY_BUILDERS
    assert UserAgentFactory._LAZY_AGENT_IDS["lelik"] == "lelik_agent"


@pytest.mark.asyncio
async def test_get_lelik_builds_on_demand_then_returns_the_registered_instance():
    agent = object()
    fake = SimpleNamespace(
        ensure_agents_for_user=AsyncMock(),
        create_agent_on_demand=AsyncMock(return_value=True),
        coordinator=MagicMock(get_agent=MagicMock(return_value=agent)),
        _LAZY_AGENT_IDS=UserAgentFactory._LAZY_AGENT_IDS,
    )
    assert await UserAgentFactory.get_lelik(fake, "u1") is agent
    fake.create_agent_on_demand.assert_awaited_once_with("lelik", "u1")
    fake.coordinator.get_agent.assert_called_once_with("lelik_agent_u1")


@pytest.mark.asyncio
async def test_get_lelik_is_none_when_it_cannot_be_built():
    fake = SimpleNamespace(ensure_agents_for_user=AsyncMock(),
                           create_agent_on_demand=AsyncMock(return_value=False),
                           coordinator=MagicMock(), _LAZY_AGENT_IDS=UserAgentFactory._LAZY_AGENT_IDS)
    assert await UserAgentFactory.get_lelik(fake, "u1") is None


@pytest.mark.asyncio
async def test_get_lelik_refreshes_the_users_agents_before_building():
    order = []
    fake = SimpleNamespace(
        ensure_agents_for_user=AsyncMock(side_effect=lambda uid: order.append(("ensure", uid))),
        create_agent_on_demand=AsyncMock(side_effect=lambda t, uid: order.append(("create", t, uid)) or True),
        coordinator=MagicMock(), _LAZY_AGENT_IDS=UserAgentFactory._LAZY_AGENT_IDS,
    )
    await UserAgentFactory.get_lelik(fake, "u1")
    assert order == [("ensure", "u1"), ("create", "lelik", "u1")]


def test_alek_gateway_is_a_lazy_builder_with_the_notification_service():
    fake = SimpleNamespace(notification_service=MagicMock())
    agent = UserAgentFactory._build_alek_gateway(fake, "u1", _ctx())
    assert agent.agent_id == "alek_agent_u1"
    assert agent._notifications is fake.notification_service
    assert UserAgentFactory._LAZY_AGENT_IDS["alek"] == "alek_agent"
