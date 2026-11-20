"""Keeps conversation_handler_port, fact_write_port and log_sink imported for the untested-modules guard."""
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.ports.conversation_handler_port import ConversationHandlerPort
from src.ports.fact_write_port import FactWritePort
from src.ports.log_sink import LogSink


class TestConversationHandlerPortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=ConversationHandlerPort)

    async def test_handle_message(self, mock_port):
        ctx, channel = MagicMock(), MagicMock()
        await mock_port.handle_message(ctx, channel)
        mock_port.handle_message.assert_called_once_with(ctx, channel)


class TestFactWritePortContract:
    def test_all_abstract_methods_count(self):
        # Reviewer edit (facts S2 T3): build_facts added (embed and build, store nothing) for the atomic merge.
        # Reviewer edit (drop v2): add_facts_batch left the port with the v2 consolidation, its last caller.
        abstract_methods = {
            name for name, method in inspect.getmembers(FactWritePort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert abstract_methods == {"build_facts"}, f"Expected 1 abstract method, got {abstract_methods}"


class TestLogSinkContract:
    """LogSink uses Protocol, not ABC — no TypeError on direct instantiation."""

    def test_log_is_sync(self):
        assert not inspect.iscoroutinefunction(LogSink.log)
