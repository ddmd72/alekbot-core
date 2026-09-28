from types import SimpleNamespace
from unittest.mock import MagicMock

from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.user import UserBotConfig


def test_alek_gateway_is_built_with_the_600_s_ask_alek_timeout():
    profile = MagicMock()
    profile.config = UserBotConfig(timezone="Europe/Madrid")
    fake = SimpleNamespace(notification_service=MagicMock())
    agent = UserAgentFactory._build_alek_gateway(fake, "u1", _UserContext(user_profile=profile,
                                                                            prompt_builder=MagicMock()))
    assert agent.config.timeout_ms == 600_000
