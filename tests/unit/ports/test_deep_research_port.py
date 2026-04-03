"""
Port contract tests for DeepResearchPort.

Covers:
- DeepResearchPort (2 abstract methods: create_interaction + get_status — both async)
- Model selection is adapter-internal; no get_model_for_tier() on the port.
"""

import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock

from src.ports.deep_research_port import DeepResearchPort
from src.domain.user import PerformanceTier


class TestDeepResearchPortContract:
    """Verify DeepResearchPort declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(DeepResearchPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            DeepResearchPort()

    def test_has_create_interaction(self):
        assert getattr(DeepResearchPort.create_interaction, "__isabstractmethod__", False)

    def test_has_get_status(self):
        assert getattr(DeepResearchPort.get_status, "__isabstractmethod__", False)

    def test_create_interaction_is_async(self):
        assert inspect.iscoroutinefunction(DeepResearchPort.create_interaction)

    def test_get_status_is_async(self):
        assert inspect.iscoroutinefunction(DeepResearchPort.get_status)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(DeepResearchPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 2, f"Expected 2 abstract methods, got {abstract_methods}"

    def test_create_interaction_signature(self):
        sig = inspect.signature(DeepResearchPort.create_interaction)
        params = list(sig.parameters.keys())
        assert params == ["self", "query", "user_id", "account_id", "original_query", "tier", "system_prompt", "session_id", "second_pass"]
        assert sig.parameters["tier"].default == PerformanceTier.BALANCED
        assert sig.parameters["system_prompt"].default is None
        assert sig.parameters["session_id"].default is None
        assert sig.parameters["second_pass"].default is False

    def test_get_status_signature(self):
        sig = inspect.signature(DeepResearchPort.get_status)
        params = list(sig.parameters.keys())
        assert params == ["self", "job_id"]


class TestDeepResearchPortMockImplementation:
    """Verify AsyncMock(spec=DeepResearchPort) satisfies the port contract."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=DeepResearchPort)

    async def test_create_interaction_returns_job_id(self, mock_port):
        mock_port.create_interaction.return_value = "job-abc-123"
        result = await mock_port.create_interaction(
            query="Research topic X",
            user_id="u1",
            account_id="acc1",
            original_query="Research topic X",
            tier=PerformanceTier.BALANCED,
        )
        assert isinstance(result, str)
        assert result == "job-abc-123"

    async def test_get_status_returns_in_progress(self, mock_port):
        mock_port.get_status.return_value = ("in_progress", "")
        status, payload = await mock_port.get_status("job-abc-123")
        assert status == "in_progress"
        assert payload == ""

    async def test_get_status_returns_completed(self, mock_port):
        mock_port.get_status.return_value = ("completed", "Research result text")
        status, payload = await mock_port.get_status("job-abc-123")
        assert status == "completed"
        assert isinstance(payload, str)
        assert len(payload) > 0

    async def test_get_status_returns_failed(self, mock_port):
        mock_port.get_status.return_value = ("failed", "Quota exceeded")
        status, payload = await mock_port.get_status("job-abc-123")
        assert status == "failed"
        assert "Quota" in payload
