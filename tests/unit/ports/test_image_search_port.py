"""ImageResult, the value object of the ImageSearchPort module: structure, immutability, equality."""
import pytest
from dataclasses import fields

from src.ports.image_search_port import ImageResult


# =============================================================================
# ImageResult dataclass
# =============================================================================

class TestImageResultDataclass:
    def test_can_instantiate(self):
        result = ImageResult(
            url="https://images.unsplash.com/photo-123?w=1080",
            raw_url="https://images.unsplash.com/photo-123",
            photographer="Kim",
            photographer_url="https://unsplash.com/@kim?utm_source=alekbot",
        )
        assert result.url == "https://images.unsplash.com/photo-123?w=1080"
        assert result.raw_url == "https://images.unsplash.com/photo-123"
        assert result.photographer == "Kim"
        assert result.photographer_url == "https://unsplash.com/@kim?utm_source=alekbot"

    def test_is_frozen_dataclass(self):
        result = ImageResult(
            url="https://images.unsplash.com/photo-abc",
            raw_url="https://images.unsplash.com/photo-abc",
            photographer="Jordan",
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
            photographer="Clara",
            photographer_url="https://unsplash.com/@clara",
        )
        b = ImageResult(
            url="https://images.unsplash.com/photo-1",
            raw_url="https://images.unsplash.com/photo-1-raw",
            photographer="Clara",
            photographer_url="https://unsplash.com/@clara",
        )
        assert a == b

    def test_inequality_when_fields_differ(self):
        a = ImageResult(
            url="https://images.unsplash.com/photo-1",
            raw_url="https://images.unsplash.com/photo-1-raw",
            photographer="Clara",
            photographer_url="https://unsplash.com/@clara",
        )
        b = ImageResult(
            url="https://images.unsplash.com/photo-2",
            raw_url="https://images.unsplash.com/photo-2-raw",
            photographer="Robin",
            photographer_url="https://unsplash.com/@robin",
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
