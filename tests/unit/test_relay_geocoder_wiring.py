"""The relay's reverse geocoder (GEO_MARKS_RFC §4.7): built from the optional `GOOGLE_SEARCH_API_KEY`, absent without it,
and handed to the web `VoiceSessionService` only (the phone service gets none)."""
import asyncio
from typing import Any, Dict, List

import pytest

import relay_main
from src.adapters.google_geocoding_adapter import GoogleGeocodingAdapter


def test_a_key_builds_the_adapter(monkeypatch):
    monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", " placeholder-key \n")
    assert isinstance(relay_main._reverse_geocoder(), GoogleGeocodingAdapter)


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_no_key_means_no_geocoder(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("GOOGLE_SEARCH_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", raw)
    assert relay_main._reverse_geocoder() is None


class _Stop(Exception):
    pass


def test_main_gives_the_geocoder_to_the_web_service_only(monkeypatch):
    built: List[Dict[str, Any]] = []

    class _RecordingService:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            if len(built) == 2:
                raise _Stop

    monkeypatch.setattr(relay_main, "build_memory_telemetry", lambda name: None)
    monkeypatch.setattr(relay_main, "VoiceSessionService", _RecordingService)
    for key, value in {"CLOUD_RUN_SERVICE_URL": "https://main.example.test", "OPENAI_API_KEY": "placeholder-key",
                       "XAI_API_KEY": "placeholder-key",
                       "BILLING_SLACK_WEBHOOK_URL": "https://hooks.example.test/placeholder",
                       "GOOGLE_SEARCH_API_KEY": "placeholder-key"}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(_Stop):
        asyncio.run(relay_main.main())
    phone, web = built
    assert "control" not in phone and isinstance(web["control"].reverse_geocoder, GoogleGeocodingAdapter)
