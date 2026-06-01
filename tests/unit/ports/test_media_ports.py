"""
Port contract tests for media-related ports.

Covers:
- MediaStoragePort (1 abstract method: store)
- PlatformMediaPort (2 abstract methods: upload_image, upload_file)
"""

import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock

from src.ports.media_storage_port import MediaStoragePort
from src.ports.platform_media_port import PlatformMediaPort


# =============================================================================
# MediaStoragePort
# =============================================================================

class TestMediaStoragePortContract:
    """Verify MediaStoragePort port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(MediaStoragePort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            MediaStoragePort()

    def test_has_store(self):
        assert getattr(MediaStoragePort.store, "__isabstractmethod__", False)

    def test_store_is_async(self):
        assert inspect.iscoroutinefunction(MediaStoragePort.store)

    def test_has_fetch(self):
        assert getattr(MediaStoragePort.fetch, "__isabstractmethod__", False)

    def test_fetch_is_async(self):
        assert inspect.iscoroutinefunction(MediaStoragePort.fetch)

    def test_has_generate_signed_url(self):
        assert getattr(MediaStoragePort.generate_signed_url, "__isabstractmethod__", False)

    def test_generate_signed_url_is_async(self):
        assert inspect.iscoroutinefunction(MediaStoragePort.generate_signed_url)

    def test_generate_signed_url_signature(self):
        sig = inspect.signature(MediaStoragePort.generate_signed_url)
        assert list(sig.parameters.keys()) == ["self", "key", "ttl_seconds"]

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(MediaStoragePort)
            if getattr(method, "__isabstractmethod__", False)
        }
        # store (write) + fetch (server-side read) + generate_signed_url (/f route)
        assert len(abstract_methods) == 3, f"Expected 3 abstract methods, got {abstract_methods}"

    def test_store_signature(self):
        sig = inspect.signature(MediaStoragePort.store)
        params = list(sig.parameters.keys())
        assert params == ["self", "data", "key", "content_type"]

    def test_store_return_annotation(self):
        sig = inspect.signature(MediaStoragePort.store)
        assert sig.return_annotation == str

    def test_fetch_signature(self):
        sig = inspect.signature(MediaStoragePort.fetch)
        params = list(sig.parameters.keys())
        assert params == ["self", "key"]

    def test_fetch_return_annotation(self):
        sig = inspect.signature(MediaStoragePort.fetch)
        assert sig.return_annotation == bytes


class TestMediaStoragePortMockImplementation:
    """Verify AsyncMock(spec=MediaStoragePort) satisfies the port contract."""

    @pytest.fixture
    def mock_storage(self):
        return AsyncMock(spec=MediaStoragePort)

    async def test_store_returns_url(self, mock_storage):
        mock_storage.store.return_value = "https://storage.example.com/html/file.html"
        result = await mock_storage.store(
            data=b"<html></html>",
            key="html/file.html",
            content_type="text/html; charset=utf-8",
        )
        assert isinstance(result, str)
        assert result.startswith("https://")

    async def test_store_called_with_expected_args(self, mock_storage):
        mock_storage.store.return_value = "https://example.com/image.png"
        await mock_storage.store(data=b"\x89PNG", key="images/map.png", content_type="image/png")
        mock_storage.store.assert_called_once_with(
            data=b"\x89PNG", key="images/map.png", content_type="image/png"
        )


# =============================================================================
# PlatformMediaPort
# =============================================================================

class TestPlatformMediaPortContract:
    """Verify PlatformMediaPort port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(PlatformMediaPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PlatformMediaPort()

    def test_has_upload_image(self):
        assert getattr(PlatformMediaPort.upload_image, "__isabstractmethod__", False)

    def test_has_upload_file(self):
        assert getattr(PlatformMediaPort.upload_file, "__isabstractmethod__", False)

    def test_both_methods_are_async(self):
        assert inspect.iscoroutinefunction(PlatformMediaPort.upload_image)
        assert inspect.iscoroutinefunction(PlatformMediaPort.upload_file)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(PlatformMediaPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 2, f"Expected 2 abstract methods, got {abstract_methods}"

    def test_upload_image_signature(self):
        sig = inspect.signature(PlatformMediaPort.upload_image)
        params = list(sig.parameters.keys())
        assert params == ["self", "image_bytes", "alt_text", "channel_id"]

    def test_upload_file_signature(self):
        sig = inspect.signature(PlatformMediaPort.upload_file)
        params = list(sig.parameters.keys())
        assert params == ["self", "file_bytes", "filename", "title", "channel_id"]


class TestPlatformMediaPortMockImplementation:
    """Verify AsyncMock(spec=PlatformMediaPort) satisfies the port contract."""

    @pytest.fixture
    def mock_media(self):
        return AsyncMock(spec=PlatformMediaPort)

    async def test_upload_image_called(self, mock_media):
        await mock_media.upload_image(
            image_bytes=b"\x89PNG", alt_text="Map", channel_id="C123"
        )
        mock_media.upload_image.assert_called_once_with(
            image_bytes=b"\x89PNG", alt_text="Map", channel_id="C123"
        )

    async def test_upload_file_called(self, mock_media):
        await mock_media.upload_file(
            file_bytes=b"content",
            filename="report.md",
            title="Weekly Report",
            channel_id="C456",
        )
        mock_media.upload_file.assert_called_once_with(
            file_bytes=b"content",
            filename="report.md",
            title="Weekly Report",
            channel_id="C456",
        )
