"""
Unit tests for PlatformPort ABC contract.

Verifies:
- PlatformPort cannot be instantiated directly
- Incomplete subclass (missing abstract methods) raises TypeError
- Complete subclass satisfies the contract
- Required abstract methods: start, stop, _translate_platform_files, get_platform_name
"""
import pytest

from src.ports.platform_port import PlatformPort
from src.domain.messaging import FileAttachment


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


class MissingStartAdapter(PlatformPort):
    """Incomplete: missing start()."""

    async def stop(self) -> None:
        pass

    async def _translate_platform_files(self, platform_files: list):
        return []

    def get_platform_name(self) -> str:
        return "test"


class MissingStopAdapter(PlatformPort):
    """Incomplete: missing stop()."""

    async def start(self) -> None:
        pass

    async def _translate_platform_files(self, platform_files: list):
        return []

    def get_platform_name(self) -> str:
        return "test"


class MissingTranslateAdapter(PlatformPort):
    """Incomplete: missing _translate_platform_files()."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def get_platform_name(self) -> str:
        return "test"


class MissingPlatformNameAdapter(PlatformPort):
    """Incomplete: missing get_platform_name()."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _translate_platform_files(self, platform_files: list):
        return []


class TestPlatformPortContract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PlatformPort()  # type: ignore

    def test_missing_start_raises(self):
        with pytest.raises(TypeError):
            MissingStartAdapter()

    def test_missing_stop_raises(self):
        with pytest.raises(TypeError):
            MissingStopAdapter()

    def test_missing_translate_raises(self):
        with pytest.raises(TypeError):
            MissingTranslateAdapter()

    def test_missing_platform_name_raises(self):
        with pytest.raises(TypeError):
            MissingPlatformNameAdapter()

    def test_complete_subclass_instantiates(self):
        adapter = CompletePlatformAdapter()
        assert isinstance(adapter, PlatformPort)

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

    def test_platform_port_is_in_ports_module(self):
        """Verify PlatformPort lives in ports/, not in adapters/."""
        import src.ports.platform_port as module
        assert hasattr(module, "PlatformPort")
