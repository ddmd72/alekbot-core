"""send_to_chat: one word for the session tool and the errand intent it runs as (VOICE_COMPANION_RFC §4.15.2,
revised 2026-10-29)."""
from src.domain.voice_errand import (
    SEND_TO_CHAT, SEND_TO_CHAT_INTENT, delegation_arguments, is_errand,
)


def test_the_tool_and_the_intent_are_one_word():
    assert SEND_TO_CHAT == SEND_TO_CHAT_INTENT == "send_to_chat"


def test_a_send_to_chat_call_runs_as_the_errand_intent_with_its_request_as_the_query():
    args = delegation_arguments("send_to_chat", {"request": "the garage rules for guests, as a file"})

    assert args == {"intent": "send_to_chat", "query": "the garage rules for guests, as a file"}
    assert is_errand(args)


def test_a_send_to_chat_call_without_a_request_still_runs_as_an_errand():
    args = delegation_arguments("send_to_chat", {})

    assert args == {"intent": "send_to_chat", "query": ""}
    assert is_errand(args)


def test_an_ask_alek_delegation_passes_through_unchanged_and_is_no_errand():
    original = {"intent": "ask_alek", "query": "what do the garage rules say about guests?"}

    args = delegation_arguments("delegate_to_specialist", original)

    assert args == original and args is not original
    assert not is_errand(args)


def test_a_call_with_no_tool_name_passes_through():
    assert delegation_arguments(None, {"intent": "search_memory", "query": "q"}) == {
        "intent": "search_memory", "query": "q"}
