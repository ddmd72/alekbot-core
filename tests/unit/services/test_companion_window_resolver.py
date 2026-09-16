from unittest.mock import AsyncMock

import pytest

from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.services.channel_binding_service import ChannelBindingService
from src.services.companion_window_resolver import CompanionWindowResolver


@pytest.fixture
def bindings():
    return AsyncMock(spec=ChannelBindingService)


@pytest.fixture
def resolver(bindings):
    return CompanionWindowResolver(channel_binding_service=bindings)


async def test_no_colon_in_session_id_returns_none(resolver, bindings):
    result = await resolver.resolve("no-colon-here")
    assert result is None
    bindings.get.assert_not_called()


async def test_unbound_channel_returns_none(resolver, bindings):
    bindings.get.return_value = None
    result = await resolver.resolve("slack:C999")
    assert result is None
    bindings.get.assert_called_once_with("C999")


async def test_bound_channel_without_companion_config_returns_none(resolver, bindings):
    bindings.get.return_value = ChannelBinding(
        channel_id="C1", agent_type="tutor", intent="chat", created_by="user-1",
        companion_config=None,
    )
    result = await resolver.resolve("slack:C1")
    assert result is None


async def test_bound_channel_with_companion_config_returns_override(resolver, bindings):
    bindings.get.return_value = ChannelBinding(
        channel_id="C1", agent_type="tutor", intent="chat", created_by="user-1",
        companion_config=CompanionConfig(window_threshold=100, batch_size=50),
    )
    result = await resolver.resolve("slack:C1")
    assert result == (100, 50)


async def test_alek_session_id_shape_also_supported(resolver, bindings):
    bindings.get.return_value = None
    result = await resolver.resolve("user-abc:C1")
    assert result is None
    bindings.get.assert_called_once_with("C1")


async def test_lookup_exception_returns_none(resolver, bindings):
    bindings.get.side_effect = Exception("firestore down")
    result = await resolver.resolve("slack:C1")
    assert result is None
