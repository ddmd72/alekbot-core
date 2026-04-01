"""
Port contract tests for FileStoragePort.

Covers:
- FileStoragePort (5 abstract methods: upload, download, delete, exists, get_url — all async)
"""

import inspect
import pytest
from abc import ABC

from src.ports.file_storage_port import FileStoragePort


class TestFileStoragePortContract:
    """Verify FileStoragePort declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(FileStoragePort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            FileStoragePort()

    def test_upload_is_abstract(self):
        assert getattr(FileStoragePort.upload, "__isabstractmethod__", False)

    def test_download_is_abstract(self):
        assert getattr(FileStoragePort.download, "__isabstractmethod__", False)

    def test_delete_is_abstract(self):
        assert getattr(FileStoragePort.delete, "__isabstractmethod__", False)

    def test_exists_is_abstract(self):
        assert getattr(FileStoragePort.exists, "__isabstractmethod__", False)

    def test_get_url_is_abstract(self):
        assert getattr(FileStoragePort.get_url, "__isabstractmethod__", False)

    def test_upload_is_async(self):
        assert inspect.iscoroutinefunction(FileStoragePort.upload)

    def test_download_is_async(self):
        assert inspect.iscoroutinefunction(FileStoragePort.download)

    def test_delete_is_async(self):
        assert inspect.iscoroutinefunction(FileStoragePort.delete)

    def test_exists_is_async(self):
        assert inspect.iscoroutinefunction(FileStoragePort.exists)

    def test_get_url_is_async(self):
        assert inspect.iscoroutinefunction(FileStoragePort.get_url)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name
            for name, method in vars(FileStoragePort).items()
            if getattr(method, "__isabstractmethod__", False)
        }
        assert abstract_methods == {"upload", "download", "delete", "exists", "get_url"}
