"""call_event_text — the system half of a call's history pair."""
from datetime import datetime, timedelta, timezone

from src.domain.voice_call_note import call_event_text

_START = datetime(2026, 9, 22, 20, 7, 56, tzinfo=timezone.utc)


def test_names_lelik_and_the_window_in_the_users_timezone():
    text = call_event_text(_START, _START + timedelta(seconds=90), "Europe/Madrid")
    assert text.startswith("[System: phone call with Lelik, 22:07–22:09 (2 min).")
    assert "posted to the user's chat" in text


def test_short_call_reads_under_one_minute():
    assert "(under 1 min)" in call_event_text(_START, _START + timedelta(seconds=20), "UTC")


def test_missing_times_still_produce_an_unmistakable_event():
    text = call_event_text(None, None, "Europe/Madrid")
    assert text.startswith("[System: phone call with Lelik ended.")


def test_unknown_timezone_falls_back_to_utc():
    assert "20:07–" in call_event_text(_START, _START + timedelta(minutes=3), "Not/AZone")


def test_window_spans_first_turn_start_to_last_turn_end():
    from src.domain.voice_call_note import call_event_from_turns
    turns = [
        {"started_at": "2026-09-22T20:08:30+00:00", "ended_at": "2026-09-22T20:08:40+00:00"},
        {"started_at": "2026-09-22T20:07:56+00:00", "ended_at": "2026-09-22T20:08:10+00:00"},
        {"started_at": "2026-09-22T20:09:00+00:00", "ended_at": "2026-09-22T20:09:26+00:00"},
    ]
    assert "22:07–22:09 (2 min)" in call_event_from_turns(turns, "Europe/Madrid")


def test_no_turns_means_no_window():
    from src.domain.voice_call_note import call_event_from_turns
    assert call_event_from_turns([], "UTC").startswith("[System: phone call with Lelik ended.")
