"""
Unit tests for ChannelBindingService.

Tests caching behaviour, CRUD operations, and cache invalidation.
"""

import pytest
import time
from unittest.mock import AsyncMock

from src.domain.channel_binding import ChannelBinding
from src.ports.channel_binding_port import ChannelBindingPort
from src.services.channel_binding_service import ChannelBindingService


@pytest.fixture
def mock_port():
    port = AsyncMock(spec=ChannelBindingPort)
    port.get.return_value = None
    return port


@pytest.fixture
def service(mock_port):
    return ChannelBindingService(port=mock_port)


def _make_binding(channel_id="C123", agent_type="translator", intent="translate"):
    return ChannelBinding(
        channel_id=channel_id,
        agent_type=agent_type,
        intent=intent,
        created_by="user_abc",
    )


class TestGet:
    async def test_returns_none_when_not_bound(self, service, mock_port):
        result = await service.get("C123")
        assert result is None
        mock_port.get.assert_awaited_once_with("C123")

    async def test_returns_binding_from_port(self, service, mock_port):
        binding = _make_binding()
        mock_port.get.return_value = binding
        result = await service.get("C123")
        assert result is binding

    async def test_caches_result(self, service, mock_port):
        binding = _make_binding()
        mock_port.get.return_value = binding

        await service.get("C123")
        await service.get("C123")

        # Port called only once — second call served from cache
        assert mock_port.get.await_count == 1

    async def test_caches_none_result(self, service, mock_port):
        mock_port.get.return_value = None

        await service.get("C123")
        await service.get("C123")

        assert mock_port.get.await_count == 1

    async def test_cache_expires(self, service, mock_port):
        mock_port.get.return_value = _make_binding()
        await service.get("C123")

        # Expire cache entry
        service._cache["C123"] = (service._cache["C123"][0], time.time() - 400)

        await service.get("C123")
        assert mock_port.get.await_count == 2


class TestBind:
    async def test_saves_to_port(self, service, mock_port):
        binding = _make_binding()
        await service.bind(binding)
        mock_port.save.assert_awaited_once_with(binding)

    async def test_updates_cache(self, service, mock_port):
        binding = _make_binding()
        await service.bind(binding)

        # Should serve from cache without port call
        result = await service.get("C123")
        assert result is binding
        mock_port.get.assert_not_awaited()


class TestUnbind:
    async def test_deletes_from_port(self, service, mock_port):
        await service.unbind("C123")
        mock_port.delete.assert_awaited_once_with("C123")

    async def test_caches_none_after_unbind(self, service, mock_port):
        # First bind
        binding = _make_binding()
        await service.bind(binding)

        # Then unbind
        await service.unbind("C123")

        # get() should return None from cache, not call port
        result = await service.get("C123")
        assert result is None
        mock_port.get.assert_not_awaited()


class TestInvalidate:
    async def test_invalidate_forces_port_call(self, service, mock_port):
        mock_port.get.return_value = _make_binding()
        await service.get("C123")

        service.invalidate("C123")

        await service.get("C123")
        assert mock_port.get.await_count == 2


class TestChannelBindingPortContract:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            ChannelBindingPort()

    def test_has_required_methods(self):
        assert hasattr(ChannelBindingPort, "get")
        assert hasattr(ChannelBindingPort, "save")
        assert hasattr(ChannelBindingPort, "delete")


class TestChannelBindingDomain:
    def test_frozen(self):
        binding = _make_binding()
        with pytest.raises(AttributeError):
            binding.agent_type = "other"

    def test_fields(self):
        binding = _make_binding()
        assert binding.channel_id == "C123"
        assert binding.agent_type == "translator"
        assert binding.intent == "translate"
        assert binding.created_by == "user_abc"
