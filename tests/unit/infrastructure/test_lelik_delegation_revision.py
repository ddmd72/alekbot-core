"""Lelik's specialists after the 2026-09-29 revision (VOICE_COMPANION_RFC §4.15)."""
from src.infrastructure.agent_manifest import (
    ALEK, ALL_DESCRIPTORS, LELIK, SMART_RESPONSE, WEB_SEARCH_LIGHT, Intent,
)
from src.infrastructure.agent_registry import AgentRegistry, ExecutionMode


def _registry():
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    return registry


def test_lelik_has_a_light_search_and_both_alek_intents_but_no_full_web_search():
    assert LELIK.allowed_intents == frozenset(
        {Intent.SEARCH_MEMORY, Intent.SEARCH_WEB_LIGHT, Intent.ASK_ALEK, Intent.TELL_ALEK})
    assert LELIK.intent_fanout == {}


def test_tell_alek_is_an_async_errand_on_the_alek_gateway():
    assert ALEK.capabilities[Intent.TELL_ALEK] is ExecutionMode.ASYNC
    assert ALEK.capabilities[Intent.ASK_ALEK] is ExecutionMode.SYNC
    assert ALEK.capability_descriptions[Intent.TELL_ALEK]
    # Outlasts the gateway's own 600 s ceiling on the Cloud Task.
    assert ALEK.dispatch_deadline_s and ALEK.dispatch_deadline_s > 600


def test_light_search_is_internal_lazy_and_registered():
    assert WEB_SEARCH_LIGHT in ALL_DESCRIPTORS
    assert WEB_SEARCH_LIGHT.internal is True and WEB_SEARCH_LIGHT.eager is False
    assert WEB_SEARCH_LIGHT.capabilities == {Intent.SEARCH_WEB_LIGHT: ExecutionMode.SYNC}


def test_lelik_is_offered_the_new_intents_and_smart_is_not():
    registry = _registry()
    lelik = {i["name"] for i in registry.get_available_intents_for(LELIK)}
    smart = {i["name"] for i in registry.get_available_intents_for(SMART_RESPONSE)}

    assert lelik == {"search_memory", "search_web_light", "ask_alek", "tell_alek"}
    assert not smart & {"search_web_light", "ask_alek", "tell_alek"}
