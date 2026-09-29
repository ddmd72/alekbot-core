"""The ask_alek commission text (VOICE_COMPANION_RFC §4.15.4)."""
from src.agents.alek_gateway_agent import _commission


def test_delegation_timestamp_is_stripped_because_smart_stamps_the_turn_itself():
    text = _commission("[Sep 28, 10:20 UTC] Remind the user to leave for the airport", None, [])

    assert text.startswith("Remind the user to leave for the airport")
    assert "UTC]" not in text


def test_a_query_without_a_timestamp_is_kept_verbatim():
    assert _commission("What is on tomorrow?", None, []).startswith("What is on tomorrow?")


def test_call_context_is_marked_as_the_source_of_truth_over_lelik_paraphrase():
    text = _commission(
        "Remind them at 13:45 with a 45-60 min buffer",
        None,
        [{"role": "user", "text": "Remind me when to leave to meet her"}],
    )

    assert "weaker model and may be imprecise" in text
    assert "their words win over Lelik's" in text
    assert text.index("their words win") < text.index("user: Remind me when to leave to meet her")


def test_no_call_context_means_no_source_of_truth_note():
    assert "weaker model" not in _commission("q", None, [])


def test_the_block_still_closes_after_lelik_note():
    text = _commission("q", "caller is driving", [{"role": "user", "text": "hi"}])

    assert text.endswith("Lelik's note: caller is driving]")
