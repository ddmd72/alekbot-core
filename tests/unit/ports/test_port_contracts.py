"""
Unit tests for port contract completeness.

Verifies that ABC port interfaces declare all methods that adapters implement,
and that all abstract methods are properly decorated with @abstractmethod.
"""

import inspect
import pytest
from abc import ABC
from typing import List, Optional
from unittest.mock import AsyncMock

from src.ports.consolidation_queue import ConsolidationQueue
from src.ports.session_store import SessionStore
from src.domain.consolidation import ConsolidationBatch, BatchStatus
from src.domain.llm import Message, MessagePart
from src.domain.session import SessionState


# =============================================================================
# ConsolidationQueue Port Contract Tests
# =============================================================================

class TestConsolidationQueueContract:
    """Verify ConsolidationQueue port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(ConsolidationQueue, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            ConsolidationQueue()

    def test_has_enqueue_batch(self):
        assert hasattr(ConsolidationQueue, "enqueue_batch")
        assert getattr(ConsolidationQueue.enqueue_batch, "__isabstractmethod__", False)

    def test_has_get_pending_batches(self):
        assert hasattr(ConsolidationQueue, "get_pending_batches")
        assert getattr(ConsolidationQueue.get_pending_batches, "__isabstractmethod__", False)

    def test_has_get_queue_size(self):
        assert hasattr(ConsolidationQueue, "get_queue_size")
        assert getattr(ConsolidationQueue.get_queue_size, "__isabstractmethod__", False)

    def test_has_delete_batch(self):
        assert hasattr(ConsolidationQueue, "delete_batch")
        assert getattr(ConsolidationQueue.delete_batch, "__isabstractmethod__", False)

    def test_has_cleanup_old_batches(self):
        assert hasattr(ConsolidationQueue, "cleanup_old_batches")
        assert getattr(ConsolidationQueue.cleanup_old_batches, "__isabstractmethod__", False)

    def test_has_update_batch_status(self):
        assert hasattr(ConsolidationQueue, "update_batch_status")
        assert getattr(ConsolidationQueue.update_batch_status, "__isabstractmethod__", False)

    def test_has_increment_attempts(self):
        assert hasattr(ConsolidationQueue, "increment_attempts")
        assert getattr(ConsolidationQueue.increment_attempts, "__isabstractmethod__", False)

    def test_no_duplicate_methods(self):
        """Ensure no method is defined more than once (regression test for duplicate bug)."""
        source = inspect.getsource(ConsolidationQueue)
        for method_name in [
            "enqueue_batch", "get_pending_batches", "get_queue_size",
            "delete_batch", "cleanup_old_batches", "update_batch_status",
            "increment_attempts",
        ]:
            count = source.count(f"async def {method_name}(")
            assert count == 1, f"{method_name} defined {count} times, expected 1"

    def test_all_abstract_methods_count(self):
        """Port should have exactly 8 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(ConsolidationQueue)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 8, f"Expected 8 abstract methods, got {abstract_methods}"

    def test_enqueue_batch_signature(self):
        sig = inspect.signature(ConsolidationQueue.enqueue_batch)
        params = list(sig.parameters.keys())
        assert params == ["self", "batch"]
        assert sig.return_annotation == str

    def test_get_pending_batches_signature(self):
        sig = inspect.signature(ConsolidationQueue.get_pending_batches)
        params = list(sig.parameters.keys())
        assert params == ["self", "user_id", "limit"]
        assert sig.parameters["user_id"].default is None
        assert sig.parameters["limit"].default == 10


class TestConsolidationQueueMockImplementation:
    """Verify a mock implementation can satisfy the port contract."""

    @pytest.fixture
    def mock_queue(self):
        return AsyncMock(spec=ConsolidationQueue)

    @pytest.mark.asyncio
    async def test_enqueue_batch(self, mock_queue):
        batch = ConsolidationBatch(user_id="u1", session_id="s1", messages=[])
        mock_queue.enqueue_batch.return_value = batch.batch_id
        result = await mock_queue.enqueue_batch(batch)
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_get_pending_batches(self, mock_queue):
        mock_queue.get_pending_batches.return_value = []
        result = await mock_queue.get_pending_batches(user_id="u1")
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_get_queue_size(self, mock_queue):
        mock_queue.get_queue_size.return_value = 42
        result = await mock_queue.get_queue_size("u1")
        assert result == 42

    @pytest.mark.asyncio
    async def test_update_batch_status(self, mock_queue):
        await mock_queue.update_batch_status("b1", BatchStatus.COMPLETED)
        mock_queue.update_batch_status.assert_called_once_with("b1", BatchStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_delete_batch(self, mock_queue):
        await mock_queue.delete_batch("b1")
        mock_queue.delete_batch.assert_called_once_with("b1")

    @pytest.mark.asyncio
    async def test_cleanup_old_batches(self, mock_queue):
        mock_queue.cleanup_old_batches.return_value = 3
        result = await mock_queue.cleanup_old_batches("u1", max_messages=600)
        assert result == 3

    @pytest.mark.asyncio
    async def test_increment_attempts(self, mock_queue):
        mock_queue.increment_attempts.return_value = 2
        result = await mock_queue.increment_attempts("b1")
        assert result == 2


# =============================================================================
# SessionStore Port Contract Tests
# =============================================================================

class TestSessionStoreContract:
    """Verify SessionStore port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(SessionStore, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            SessionStore()

    def test_has_load_session(self):
        assert hasattr(SessionStore, "load_session")
        assert getattr(SessionStore.load_session, "__isabstractmethod__", False)

    def test_has_save_session(self):
        assert hasattr(SessionStore, "save_session")
        assert getattr(SessionStore.save_session, "__isabstractmethod__", False)

    def test_has_append_message(self):
        assert hasattr(SessionStore, "append_message")
        assert getattr(SessionStore.append_message, "__isabstractmethod__", False)

    def test_has_append_messages_batch(self):
        """Regression test: append_messages_batch must be @abstractmethod."""
        assert hasattr(SessionStore, "append_messages_batch")
        assert getattr(SessionStore.append_messages_batch, "__isabstractmethod__", False), \
            "append_messages_batch must be decorated with @abstractmethod"

    def test_has_get_latest_session_id(self):
        assert hasattr(SessionStore, "get_latest_session_id")
        assert getattr(SessionStore.get_latest_session_id, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 5 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(SessionStore)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"

    def test_append_messages_batch_signature(self):
        sig = inspect.signature(SessionStore.append_messages_batch)
        params = list(sig.parameters.keys())
        assert params == ["self", "session_id", "messages", "owner_id"]
        assert sig.parameters["owner_id"].default is None


class TestSessionStoreMockImplementation:
    """Verify a mock implementation can satisfy the port contract."""

    @pytest.fixture
    def mock_store(self):
        return AsyncMock(spec=SessionStore)

    @pytest.mark.asyncio
    async def test_load_session(self, mock_store):
        mock_store.load_session.return_value = SessionState(session_id="s1")
        result = await mock_store.load_session("s1")
        assert result.session_id == "s1"

    @pytest.mark.asyncio
    async def test_save_session(self, mock_store):
        state = SessionState(session_id="s1")
        await mock_store.save_session("s1", state)
        mock_store.save_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_message(self, mock_store):
        msg = Message(role="user", parts=[MessagePart(text="hello")])
        await mock_store.append_message("s1", msg, owner_id="u1")
        mock_store.append_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_messages_batch(self, mock_store):
        msgs = [
            Message(role="user", parts=[MessagePart(text="hello")]),
            Message(role="model", parts=[MessagePart(text="hi")]),
        ]
        await mock_store.append_messages_batch("s1", msgs, owner_id="u1")
        mock_store.append_messages_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_latest_session_id(self, mock_store):
        mock_store.get_latest_session_id.return_value = "s1"
        result = await mock_store.get_latest_session_id("u1")
        assert result == "s1"
