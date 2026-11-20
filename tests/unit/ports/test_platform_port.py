"""Unit tests for PlatformPort: a complete implementation is driven through the port's methods."""
import pytest

from src.ports.platform_port import PlatformPort


class CompletePlatformAdapter(PlatformPort):
    """Minimal concrete implementation satisfying all abstract methods."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _translate_platform_files(self, platform_files: list):
        return []

    def get_platform_name(self) -> str:
        return "test"


class TestPlatformPortContract:
    def test_get_platform_name(self):
        adapter = CompletePlatformAdapter()
        assert adapter.get_platform_name() == "test"

    @pytest.mark.asyncio
    async def test_start_and_stop_are_async(self):
        adapter = CompletePlatformAdapter()
        await adapter.start()
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_translate_platform_files_returns_list(self):
        adapter = CompletePlatformAdapter()
        result = await adapter._translate_platform_files([])
        assert result == []
