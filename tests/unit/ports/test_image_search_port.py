"""
Port contract tests for ImageSearchPort.

Covers:
- ABC structure + abstract method enforcement
- search() signature contract
- ImageResult dataclass structure and immutability
- AsyncMock(spec=ImageSearchPort) usability
- Concrete subclass acceptance / rejection
"""
import inspect
import pytest
from abc import ABC
from dataclasses import fields
from unittest.mock import AsyncMock

from src.ports.image_search_port import ImageResult, ImageSearchPort


# =============================================================================
# ImageSearchPort ABC contract
# =============================================================================

class TestImageSearchPortContract:

    def test_is_abstract_class(self):
        assert issubclass(ImageSearchPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            ImageSearchPort()

    def test_has_search_abstract_method(self):
        assert getattr(ImageSearchPort.search, "__isabstractmethod__", False)

    def test_search_is_async(self):
        assert inspect.iscoroutinefunction(ImageSearchPort.search)

    def test_exactly_one_abstract_method(self):
        abstract = {
            name for name, method in inspect.getmembers(ImageSearchPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert abstract == {"search"}, f"Expected only search, got: {abstract}"

    def test_search_signature(self):
        sig = inspect.signature(ImageSearchPort.search)
        params = list(sig.parameters.keys())
        assert params == ["self", "query", "count"]

    def test_search_count_has_default(self):
        sig = inspect.signature(ImageSearchPort.search)
        assert sig.parameters["count"].default == 1

    def test_concrete_subclass_without_search_cannot_instantiate(self):
        class Incomplete(ImageSearchPort):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_concrete_subclass_with_search_instantiates(self):
        class Complete(ImageSearchPort):
            async def search(self, query: str, count: int = 1) -> list[ImageResult]:
                return []

        instance = Complete()
        assert isinstance(instance, ImageSearchPort)


# =============================================================================
# ImageResult dataclass
# =============================================================================

class TestImageResultDataclass:

    def test_can_instantiate(self):
        result = ImageResult(
            url="https://images.unsplash.com/photo-123?w=1080",
            raw_url="https://images.unsplash.com/photo-123",
            photographer="Jane Smith",
            photographer_url="https://unsplash.com/@janesmith?utm_source=alekbot",
        )
        assert result.url == "https://images.unsplash.com/photo-123?w=1080"
        assert result.raw_url == "https://images.unsplash.com/photo-123"
        assert result.photographer == "Jane Smith"
        assert result.photographer_url == "https://unsplash.com/@janesmith?utm_source=alekbot"

    def test_is_frozen_dataclass(self):
        result = ImageResult(
            url="https://images.unsplash.com/photo-abc",
            raw_url="https://images.unsplash.com/photo-abc",
            photographer="John Doe",
            photographer_url="https://unsplash.com/@johndoe",
        )
        with pytest.raises((AttributeError, TypeError)):
            result.url = "https://other.example.com/photo.jpg"

    def test_has_four_fields(self):
        field_names = {f.name for f in fields(ImageResult)}
        assert field_names == {"url", "raw_url", "photographer", "photographer_url"}

    def test_equality_by_value(self):
        a = ImageResult(
            url="https://images.unsplash.com/photo-1",
            raw_url="https://images.unsplash.com/photo-1-raw",
            photographer="Alice",
            photographer_url="https://unsplash.com/@alice",
        )
        b = ImageResult(
            url="https://images.unsplash.com/photo-1",
            raw_url="https://images.unsplash.com/photo-1-raw",
            photographer="Alice",
            photographer_url="https://unsplash.com/@alice",
        )
        assert a == b

    def test_inequality_when_fields_differ(self):
        a = ImageResult(
            url="https://images.unsplash.com/photo-1",
            raw_url="https://images.unsplash.com/photo-1-raw",
            photographer="Alice",
            photographer_url="https://unsplash.com/@alice",
        )
        b = ImageResult(
            url="https://images.unsplash.com/photo-2",
            raw_url="https://images.unsplash.com/photo-2-raw",
            photographer="Bob",
            photographer_url="https://unsplash.com/@bob",
        )
        assert a != b

    def test_is_hashable(self):
        result = ImageResult(
            url="https://images.unsplash.com/photo-abc",
            raw_url="https://images.unsplash.com/photo-abc",
            photographer="Carol",
            photographer_url="https://unsplash.com/@carol",
        )
        # frozen dataclasses are hashable — can be used in sets/dicts
        s = {result}
        assert result in s


# =============================================================================
# AsyncMock(spec=ImageSearchPort) usability
# =============================================================================

class TestImageSearchPortMockUsability:
    """AsyncMock(spec=ImageSearchPort) must satisfy the port contract."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=ImageSearchPort)

    async def test_search_returns_list(self, mock_port):
        expected = [
            ImageResult(
                url="https://images.unsplash.com/photo-1",
                raw_url="https://images.unsplash.com/photo-1-raw",
                photographer="Alice",
                photographer_url="https://unsplash.com/@alice",
            )
        ]
        mock_port.search.return_value = expected
        result = await mock_port.search("mountains fog", count=1)
        assert result == expected

    async def test_search_empty_list(self, mock_port):
        mock_port.search.return_value = []
        result = await mock_port.search("noresults", count=5)
        assert result == []

    async def test_search_called_with_correct_args(self, mock_port):
        mock_port.search.return_value = []
        await mock_port.search("ocean waves", count=3)
        mock_port.search.assert_called_once_with("ocean waves", count=3)

    async def test_search_raises_on_error(self, mock_port):
        mock_port.search.side_effect = RuntimeError("API failure")
        with pytest.raises(RuntimeError, match="API failure"):
            await mock_port.search("mountains", count=1)
