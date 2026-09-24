"""LELIK as a standard delegating agent (VOICE_COMPANION_RFC §4.7)."""
from src.infrastructure.agent_manifest import (
    ALEK, ALL_DESCRIPTORS, LELIK, QUICK_RESPONSE, SEARCH_WEB_MAPS_FANOUT, SMART_RESPONSE, Intent,
)
from src.infrastructure.agent_registry import ExecutionMode


def test_quick_smart_and_lelik_share_one_maps_fanout():
    assert QUICK_RESPONSE.intent_fanout[Intent.SEARCH_WEB] is SEARCH_WEB_MAPS_FANOUT
    assert SMART_RESPONSE.intent_fanout[Intent.SEARCH_WEB] is SEARCH_WEB_MAPS_FANOUT
    assert LELIK.intent_fanout == {Intent.SEARCH_WEB: SEARCH_WEB_MAPS_FANOUT}


def test_maps_fanout_targets_maps_query():
    assert SEARCH_WEB_MAPS_FANOUT.intents == [Intent.MAPS_QUERY]
    assert "Maps is authoritative" in SEARCH_WEB_MAPS_FANOUT.hint


def test_lelik_allowlist_is_the_fast_specialists():
    assert LELIK.allowed_intents == frozenset({Intent.SEARCH_MEMORY, Intent.SEARCH_WEB, Intent.ASK_ALEK})
    assert LELIK.internal is True
    assert LELIK.capabilities == {}


def test_alek_is_an_internal_sync_specialist():
    assert ALEK in ALL_DESCRIPTORS
    assert ALEK.internal is True and ALEK.eager is False
    assert ALEK.capabilities == {Intent.ASK_ALEK: ExecutionMode.SYNC}
    assert ALEK.capability_descriptions[Intent.ASK_ALEK]
