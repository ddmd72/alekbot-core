"""The live-text frames of protocol v1 (VOICE_CALL_LIVE_TEXT_RFC §4.7): `line` and `snapshot` carry `v` and the call's
`seq`, leave out every optional key that is None, and the `text` feature accepts no page command."""
import json

from src.domain.voice_control_protocol import (
    FEATURE_ASK,
    FEATURE_TEXT,
    accepted_commands,
    encode,
    line_frame,
    snapshot_frame,
)

_CALLER_TEXT = "Is this Albergo Immaginario?"
_LELIK_TEXT = "È questo l'Albergo Immaginario?"


def test_an_append_frame_has_the_rfc_shape():
    assert line_frame(41, "l7", "append", who="lelik", text=_LELIK_TEXT) == {
        "v": 1, "seq": 41, "type": "line", "line": "l7", "who": "lelik", "op": "append", "text": _LELIK_TEXT}


def test_an_append_frame_of_an_ask_reply_carries_the_kind():
    assert line_frame(41, "l7", "append", who="lelik", text=_LELIK_TEXT, kind="ask") == {
        "v": 1, "seq": 41, "type": "line", "line": "l7", "who": "lelik", "op": "append", "text": _LELIK_TEXT,
        "kind": "ask"}


def test_an_end_frame_has_a_status_and_no_who():
    assert line_frame(42, "l7", "end", status="done") == {
        "v": 1, "seq": 42, "type": "line", "line": "l7", "op": "end", "status": "done"}
    assert line_frame(42, "l7", "end", status="interrupted") == {
        "v": 1, "seq": 42, "type": "line", "line": "l7", "op": "end", "status": "interrupted"}


def test_a_caller_set_frame_has_the_rfc_shape():
    assert line_frame(43, "l8", "set", who="caller", text=_CALLER_TEXT) == {
        "v": 1, "seq": 43, "type": "line", "line": "l8", "who": "caller", "op": "set", "text": _CALLER_TEXT}
    assert line_frame(43, "l8", "set", who="caller", text=_CALLER_TEXT, kind="ask")["kind"] == "ask"


def test_a_snapshot_frame_holds_the_lines_and_no_state():
    lines = [
        {"line": "l1", "who": "caller", "text": _CALLER_TEXT},
        {"line": "l2", "who": "lelik", "text": _LELIK_TEXT, "status": "done"},
        {"line": "l3", "who": "caller", "text": "Lelik, how do I say thank you in Italian?", "kind": "ask"},
    ]
    assert snapshot_frame(45, lines) == {"v": 1, "seq": 45, "type": "snapshot", "lines": lines}


def test_an_empty_snapshot_is_still_a_snapshot():
    assert snapshot_frame(1, []) == {"v": 1, "seq": 1, "type": "snapshot", "lines": []}


def test_the_snapshot_copies_its_lines():
    line = {"line": "l1", "who": "lelik", "text": _LELIK_TEXT}
    frame = snapshot_frame(2, [line])
    line["text"] = "changed afterwards"
    assert frame["lines"][0]["text"] == _LELIK_TEXT


def test_a_line_frame_encodes_its_text_as_utf8():
    assert json.loads(encode(line_frame(3, "l2", "append", who="lelik", text=_LELIK_TEXT)))["text"] == _LELIK_TEXT
    assert "È questo" in encode(line_frame(3, "l2", "append", who="lelik", text=_LELIK_TEXT))


def test_the_text_feature_accepts_no_page_command():
    assert FEATURE_TEXT == "text"
    assert accepted_commands([FEATURE_TEXT]) == frozenset()
    assert accepted_commands([FEATURE_ASK, FEATURE_TEXT]) == frozenset({"ask_arm", "ask_disarm"})
