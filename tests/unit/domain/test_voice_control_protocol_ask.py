"""The "Ask Lelik" frames of protocol v1 (VOICE_CALL_CONTROL_CHANNEL_RFC §4.2): `ask_state {state, seq?, reason?}`
carries only the fields that apply, and a command's `seq` is read only when it is a non-negative integer."""
import pytest

from src.domain.voice_control_protocol import (
    FEATURE_ASK,
    accepted_commands,
    ask_state_frame,
    command_seq,
)


def test_ask_state_carries_only_the_fields_given():
    assert ask_state_frame("armed", seq=4) == {"type": "ask_state", "state": "armed", "seq": 4}
    assert ask_state_frame("idle", reason="answered") == {"type": "ask_state", "state": "idle", "reason": "answered"}
    assert ask_state_frame("active") == {"type": "ask_state", "state": "active"}
    assert ask_state_frame("idle", seq=0, reason="cancelled") == {
        "type": "ask_state", "state": "idle", "seq": 0, "reason": "cancelled"}


@pytest.mark.parametrize("seq, expected", [(0, 0), (12, 12), (-1, None), (True, None), ("3", None), (1.5, None)])
def test_command_seq_reads_a_non_negative_integer_only(seq, expected):
    assert command_seq({"type": "ask_arm", "seq": seq}) == expected


def test_a_command_without_seq_has_none():
    assert command_seq({"type": "ask_disarm"}) is None


def test_the_ask_feature_accepts_exactly_its_two_commands():
    assert accepted_commands([FEATURE_ASK]) == frozenset({"ask_arm", "ask_disarm"})
