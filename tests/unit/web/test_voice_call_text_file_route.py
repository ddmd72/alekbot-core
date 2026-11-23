"""`POST /api/voice/call-text/file` and the start response's `file_to_drive` flag."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.call_text_file import (
    MAX_CALL_TEXT_BODY_BYTES,
    MAX_CALL_TEXT_ENTRIES,
    CallTextFileOutcome,
)
from src.domain.media_room_caller_leg import CallerLeg
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.cabinet_session_kit import cabinet_session_for
from tests.unit.web.test_voice_web_call_app import MemoryStore, _setup

PAIR = {"source": "We have a booking under RM-55120.", "translation": "Abbiamo una prenotazione con il codice RM-55120."}
BODY = {"target": "drive", "mode": "interpreter", "duration_s": 30, "entries": [PAIR]}

AUTH: dict = {}   # the Bearer header of the app's one signed-in session, set by `_app()`


def _service(outcome=None, available=True):
    service = MagicMock()
    service.save = AsyncMock(return_value=outcome or CallTextFileOutcome(saved=True, path="Acme/Inbox/x.md"))
    service.drive_available = AsyncMock(return_value=available)
    return service


def _app(service, user="u1"):
    cabinet_sessions, _bearer = cabinet_session_for(user, "a1")
    AUTH.update(_bearer)
    room = AsyncMock()
    room.open_caller.return_value = CallerLeg("S1", "ANSWER")
    store = MemoryStore()
    app = Quart(__name__)
    app.register_blueprint(create_voice_web_call_blueprint(
        cabinet_sessions=cabinet_sessions, call_setup=_setup(store), media_room=room, ephemeral_store=store,
        relay_base_url="wss://relay.example.com", cipher=AesGcmTestCipher(), digest_key=b"k" * 32,
        call_text_files=service))
    return app


async def _post(app, body=BODY, headers=AUTH, raw=None):
    data = raw if raw is not None else json.dumps(body)
    resp = await app.test_client().post("/api/voice/call-text/file", data=data, headers=headers)
    return resp, await resp.get_json()


async def test_no_jwt_is_401():
    resp, _ = await _post(_app(_service()), headers={})
    assert resp.status_code == 401


async def test_the_owner_is_the_jwt_user_and_a_saved_file_answers_its_path():
    service = _service()
    resp, body = await _post(_app(service, user="u7"))
    assert resp.status_code == 200 and body == {"ok": True, "path": "Acme/Inbox/x.md"}
    user_id, account_id, request = service.save.await_args.args
    assert (user_id, account_id, request.target) == ("u7", "a1", "drive")


async def test_a_body_with_a_user_id_cannot_change_the_owner():
    service = _service()
    await _post(_app(service, user="u7"), {**BODY, "user_id": "someone-else"})
    assert service.save.await_args.args[0] == "u7"


async def test_no_service_is_503():
    resp, _ = await _post(_app(None))
    assert resp.status_code == 503


async def test_a_content_length_over_the_cap_is_413_and_nothing_is_saved():
    service = _service()
    resp, _ = await _post(_app(service), raw=b"x" * (MAX_CALL_TEXT_BODY_BYTES + 1))
    assert resp.status_code == 413
    service.save.assert_not_awaited()


async def test_a_real_length_over_the_cap_without_a_content_length_is_413():
    service = _service()
    app = _app(service)

    # A body whose Content-Length understates its real size (or has none) is not capped by Quart itself.
    resp = await app.test_client().post("/api/voice/call-text/file", data=b"{" + b" " * (MAX_CALL_TEXT_BODY_BYTES + 1),
                                        headers={**AUTH, "Content-Length": "10"})
    assert resp.status_code == 413
    service.save.assert_not_awaited()


async def test_too_many_entries_is_413():
    service = _service()
    resp, _ = await _post(_app(service), {**BODY, "entries": [PAIR] * (MAX_CALL_TEXT_ENTRIES + 1)})
    assert resp.status_code == 413
    service.save.assert_not_awaited()


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"target": "drive"}'])
async def test_a_malformed_body_is_400(raw):
    service = _service()
    resp, _ = await _post(_app(service), raw=raw)
    assert resp.status_code == 400
    service.save.assert_not_awaited()


async def test_a_mode_without_a_call_file_is_400():
    resp, _ = await _post(_app(_service()), {**BODY, "mode": "lelik"})
    assert resp.status_code == 400


@pytest.mark.parametrize("reason,status", [("not_connected", 409), ("refused", 409), ("no_chat", 409),
                                           ("failed", 502)])
async def test_a_failed_save_answers_its_reason(reason, status):
    resp, body = await _post(_app(_service(CallTextFileOutcome(saved=False, reason=reason))))
    assert resp.status_code == status and body == {"ok": False, "reason": reason}


async def _start(app, mode=None):
    payload = {"sdp": "OFFER", "mid": "0", **({"mode": mode} if mode else {})}
    resp = await app.test_client().post("/api/voice/web-call", json=payload, headers=AUTH)
    return resp, await resp.get_json()


@pytest.mark.parametrize("mode,available,flag", [("interpreter", True, True), ("tutor", True, True),
                                                 ("interpreter", False, False), ("lelik", True, False),
                                                 (None, True, False)])
async def test_the_start_flag_follows_the_mode_and_the_drive(mode, available, flag):
    service = _service(available=available)
    resp, body = await _start(_app(service), mode)
    assert resp.status_code == 201 and body["file_to_drive"] is flag
    if mode not in ("interpreter", "tutor"):
        service.drive_available.assert_not_awaited()


async def test_a_raising_service_still_starts_the_call_with_the_flag_false():
    service = _service()
    service.drive_available.side_effect = RuntimeError("boom")
    resp, body = await _start(_app(service), "interpreter")
    assert resp.status_code == 201 and body["file_to_drive"] is False


async def test_with_no_service_the_flag_is_false():
    resp, body = await _start(_app(None), "interpreter")
    assert resp.status_code == 201 and body["file_to_drive"] is False
