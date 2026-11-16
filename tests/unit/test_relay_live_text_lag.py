"""LIVE_TEXT_LAG_MS, the relay's optional knob (VOICE_CALL_LIVE_TEXT_RFC §4.3): an integer >= 0, else 0."""
import pytest

import relay_main


def test_unset_gives_zero(monkeypatch):
    monkeypatch.delenv("LIVE_TEXT_LAG_MS", raising=False)
    assert relay_main._live_text_lag_ms() == 0


def test_an_integer_is_used(monkeypatch):
    monkeypatch.setenv("LIVE_TEXT_LAG_MS", "120")
    assert relay_main._live_text_lag_ms() == 120


@pytest.mark.parametrize("raw", ["-5", "abc", "1.5"])
def test_anything_else_gives_zero(monkeypatch, raw):
    monkeypatch.setenv("LIVE_TEXT_LAG_MS", raw)
    assert relay_main._live_text_lag_ms() == 0
