"""relay_main.main() hands the VOICE_DEBUG_TRANSCRIPT_USERS set to BOTH session services (phone and web): the boot is
run up to the second service's construction, with placeholder env values and no network."""
import asyncio
from typing import Any, Dict, List, Optional

import pytest

import relay_main


class _Stop(Exception):
    pass


def _boot(monkeypatch, knob: Optional[str] = None) -> List[Dict[str, Any]]:
    built: List[Dict[str, Any]] = []

    class _RecordingService:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            if len(built) == 2:
                raise _Stop

    monkeypatch.setattr(relay_main, "build_memory_telemetry", lambda name: None)
    monkeypatch.setattr(relay_main, "VoiceSessionService", _RecordingService)
    for key, value in {"CLOUD_RUN_SERVICE_URL": "https://main.example.test", "OPENAI_API_KEY": "placeholder-key",
                       "XAI_API_KEY": "placeholder-key\n",
                       "BILLING_SLACK_WEBHOOK_URL": "https://hooks.example.test/placeholder"}.items():
        monkeypatch.setenv(key, value)
    if knob is None:
        monkeypatch.delenv("VOICE_DEBUG_TRANSCRIPT_USERS", raising=False)
    else:
        monkeypatch.setenv("VOICE_DEBUG_TRANSCRIPT_USERS", knob)
    with pytest.raises(_Stop):
        asyncio.run(relay_main.main())
    return built


def test_both_services_get_the_listed_users(monkeypatch):
    phone, web = _boot(monkeypatch, "user-a; user-b")
    assert phone["debug_transcript_users"] == frozenset({"user-a", "user-b"})
    assert web["debug_transcript_users"] == frozenset({"user-a", "user-b"})
    assert "control_hub" in web and "control_hub" not in phone  # the two are the phone and the web service


def test_unset_gives_both_services_an_empty_set(monkeypatch):
    phone, web = _boot(monkeypatch)
    assert phone["debug_transcript_users"] == frozenset() and web["debug_transcript_users"] == frozenset()
