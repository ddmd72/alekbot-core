from datetime import datetime, timezone

from src.domain.voice_call_note import call_event_from_turns, call_event_text

_S = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
_E = datetime(2026, 9, 27, 10, 5, tzinfo=timezone.utc)


def test_web_call_header_names_a_web_call():
    text = call_event_text(_S, _E, "UTC", call_kind="web")
    assert text.startswith("[System: web call with Lelik, 10:00–10:05 (5 min).")


def test_default_header_is_unchanged_phone_call():
    assert call_event_text(_S, _E, "UTC").startswith("[System: phone call with Lelik, 10:00–10:05")


def test_web_call_without_turns_still_names_a_web_call():
    assert call_event_from_turns([], "UTC", call_kind="web").startswith("[System: web call with Lelik ended.")
