"""
Port contract tests for MapsToolsPort.

Covers:
- MapsToolsPort (2 abstract methods: get_tool_declarations + call_tool — both async)
- MapsToolError (domain exception for tool execution failures)
- AsyncMock(spec=MapsToolsPort) satisfies the port contract in agent tests
"""

import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock

from src.ports.maps_tools_port import MapsToolError, MapsToolsPort


class TestMapsToolsPortContract:
    """Verify MapsToolsPort declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(MapsToolsPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            MapsToolsPort()

    def test_has_get_tool_declarations(self):
        assert getattr(MapsToolsPort.get_tool_declarations, "__isabstractmethod__", False)

    def test_has_call_tool(self):
        assert getattr(MapsToolsPort.call_tool, "__isabstractmethod__", False)

    def test_get_tool_declarations_is_async(self):
        assert inspect.iscoroutinefunction(MapsToolsPort.get_tool_declarations)

    def test_call_tool_is_async(self):
        assert inspect.iscoroutinefunction(MapsToolsPort.call_tool)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(MapsToolsPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 2, f"Expected 2 abstract methods, got {abstract_methods}"

    def test_get_tool_declarations_signature(self):
        sig = inspect.signature(MapsToolsPort.get_tool_declarations)
        params = list(sig.parameters.keys())
        assert params == ["self"]

    def test_call_tool_signature(self):
        sig = inspect.signature(MapsToolsPort.call_tool)
        params = list(sig.parameters.keys())
        assert params == ["self", "name", "arguments"]


class TestMapsToolError:
    """MapsToolError is a domain exception (not adapter-internal)."""

    def test_is_exception_subclass(self):
        assert issubclass(MapsToolError, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(MapsToolError, match="quota exceeded"):
            raise MapsToolError("quota exceeded")

    def test_str_representation(self):
        err = MapsToolError("location unavailable")
        assert "location unavailable" in str(err)


class TestMapsToolsPortMockImplementation:
    """Verify AsyncMock(spec=MapsToolsPort) satisfies the port contract in agent tests."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=MapsToolsPort)

    async def test_get_tool_declarations_returns_list(self, mock_port):
        mock_port.get_tool_declarations.return_value = [
            {
                "name": "places_search",
                "description": "Find places.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
        result = await mock_port.get_tool_declarations()
        assert isinstance(result, list)
        assert result[0]["name"] == "places_search"
        assert "parameters" in result[0]

    async def test_call_tool_returns_dict(self, mock_port):
        mock_port.call_tool.return_value = {
            "places": [{"name": "Кафе Центр", "address": "вул. Хрещатик 1", "rating": 4.5}]
        }
        result = await mock_port.call_tool("places_search", {"query": "кафе поблизу"})
        assert isinstance(result, dict)
        assert "places" in result

    async def test_call_tool_raises_maps_tool_error(self, mock_port):
        mock_port.call_tool.side_effect = MapsToolError("HTTP 429: quota exceeded")
        with pytest.raises(MapsToolError):
            await mock_port.call_tool("places_search", {"query": "x"})

    async def test_route_tool_returns_distance_duration(self, mock_port):
        mock_port.call_tool.return_value = {
            "distance": "540 km",
            "duration": "5 hours 30 min",
        }
        result = await mock_port.call_tool(
            "route_computation", {"origin": "Kyiv", "destination": "Lviv"}
        )
        assert "distance" in result
        assert "duration" in result

    async def test_weather_tool_returns_conditions(self, mock_port):
        mock_port.call_tool.return_value = {
            "current": {"temperature": "12°C", "condition": "partly cloudy"}
        }
        result = await mock_port.call_tool("weather_lookup", {"location": "Kyiv"})
        assert "current" in result
