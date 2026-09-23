"""PromptContentStore.flush is part of the contract but not abstract: a store that writes
synchronously has nothing pending, so the default is a no-op."""

import inspect

import pytest

from src.ports.prompt_content_store import PromptContentStore


def test_flush_is_a_coroutine_and_not_abstract():
    assert inspect.iscoroutinefunction(PromptContentStore.flush)
    assert "flush" not in PromptContentStore.__abstractmethods__


@pytest.mark.asyncio
async def test_default_flush_is_a_no_op():
    class _SyncStore(PromptContentStore):
        async def record_turn(self, **kwargs) -> None: ...

        async def record_dr_result(self, **kwargs) -> None: ...

    assert await _SyncStore().flush() is None
