"""
Contract test for the PromptContentStore port.

The port is an ABC with a single abstract method, ``record_turn`` — a best-effort,
non-blocking hot-path capture. The adapter owns record building, so the port
takes the agent's native LLM objects + identity, not a pre-built record.
"""

import inspect

import pytest

from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart
from src.ports.prompt_content_store import PromptContentStore


def test_port_cannot_be_instantiated():
    with pytest.raises(TypeError):
        PromptContentStore()  # type: ignore[abstract]


def test_record_turn_is_the_only_abstract_method():
    assert PromptContentStore.__abstractmethods__ == frozenset({"record_turn"})


def test_record_turn_is_coroutine():
    assert inspect.iscoroutinefunction(PromptContentStore.record_turn)


async def test_concrete_subclass_satisfies_contract():
    captured = []

    class _InMemoryStore(PromptContentStore):
        async def record_turn(self, **kwargs) -> None:
            captured.append(kwargs)

    store = _InMemoryStore()
    req = LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])
    resp = LLMResponse(text="yo")
    await store.record_turn(
        request=req,
        response=resp,
        agent_id="a",
        agent_type="smart",
        account_id="acct-1",
        turn=1,
        latency_ms=12.0,
        provider="claude",
    )

    assert len(captured) == 1
    assert captured[0]["agent_id"] == "a"
    assert captured[0]["turn"] == 1
