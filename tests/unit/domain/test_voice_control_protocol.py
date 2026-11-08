"""Protocol v1 frames of the call control channel (VOICE_CALL_CONTROL_CHANNEL_RFC §4.2)."""
import hashlib
import json

import pytest

from src.domain import voice_control_protocol as p


def test_the_limits_are_the_rfcs():
    assert (p.PROTOCOL_VERSION, p.MAX_INBOUND_FRAME_BYTES, p.MAX_INBOUND_FRAMES_PER_S) == (1, 1024, 10)
    assert (p.HELLO_TIMEOUT_S, p.PING_INTERVAL_S) == (5.0, 20.0)


def test_the_digest_is_plain_sha256_hex():
    assert p.control_token_digest("token-0001-invented") == hashlib.sha256(b"token-0001-invented").hexdigest()


@pytest.mark.parametrize("text, expected", [
    ('{"type":"ping"}', {"type": "ping"}),
    ('{"type":"ask_arm","seq":3}', {"type": "ask_arm", "seq": 3}),
    ("[1]", None), ('"ping"', None), ('{"type":5}', None), ("{}", None), ("{not json", None),
])
def test_parse_inbound(text, expected):
    assert p.parse_inbound(text) == expected


@pytest.mark.parametrize("v, expected", [(1, 1), (2, 2), (0, None), (True, None), ("1", None), (None, None)])
def test_hello_version(v, expected):
    assert p.hello_version({"type": "hello", "v": v}) == expected


@pytest.mark.parametrize("token, expected", [("t-1", "t-1"), ("", None), (None, None), (7, None)])
def test_hello_token(token, expected):
    assert p.hello_token({"type": "hello", "token": token}) == expected


def test_welcome_names_features_sorted_and_state_never_overrides_the_core_fields():
    frame = p.welcome_frame({"transcript", "ask"}, {"ask": "idle", "type": "spoof", "v": 9})
    assert frame == {"type": "welcome", "v": 1, "features": ["ask", "transcript"], "ask": "idle"}


def test_bye_and_pong():
    assert [p.bye_frame(r) for r in p.ByeReason] == [
        {"type": "bye", "reason": r} for r in ("version", "auth", "call_ended", "replaced")]
    assert p.pong_frame() == {"type": "pong"}


def test_only_offered_features_accept_commands():
    assert p.accepted_commands([]) == frozenset()
    assert p.accepted_commands([p.FEATURE_ASK]) == {"ask_arm", "ask_disarm"}
    assert p.accepted_commands(["transcript"]) == frozenset()       # a feature with no page commands


def test_encode_is_compact_utf8_json():
    text = p.encode({"type": "line", "text": "Olá"})
    assert text == '{"type":"line","text":"Olá"}' and json.loads(text)["text"] == "Olá"
