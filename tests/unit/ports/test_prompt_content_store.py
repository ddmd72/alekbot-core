"""
Contract test for the PromptContentStore port.

The port is an ABC: it cannot be instantiated directly, a subclass must
implement async ``store``, and that is the only abstract method.
"""

import inspect

import pytest

from src.domain.observability import PromptContentRecord
from src.ports.prompt_content_store import PromptContentStore


def test_port_cannot_be_instantiated():
    with pytest.raises(TypeError):
        PromptContentStore()  # type: ignore[abstract]


def test_store_is_the_only_abstract_method():
    assert PromptContentStore.__abstractmethods__ == frozenset({"store"})


def test_store_is_coroutine():
    assert inspect.iscoroutinefunction(PromptContentStore.store)


async def test_concrete_subclass_satisfies_contract():
    captured = []

    class _InMemoryStore(PromptContentStore):
        async def store(self, record: PromptContentRecord) -> None:
            captured.append(record)

    store = _InMemoryStore()
    rec = PromptContentRecord(agent_id="a", response_text="hi")
    await store.store(rec)

    assert captured == [rec]
