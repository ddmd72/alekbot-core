"""`listening` in protocol v1 (VOICE_CALL_CONTROL_CHANNEL_RFC §4.2, §4.4): a core page -> relay frame with no fields,
not a feature command, so no feature set accepts it and its presence changes no other frame."""
import pytest

from src.domain import voice_control_protocol as p


def test_the_frame_type_is_listening():
    assert p.LISTENING == "listening"


def test_it_parses_as_a_page_frame_with_no_fields():
    assert p.parse_inbound('{"type":"listening"}') == {"type": "listening"}


@pytest.mark.parametrize("features", [(), (p.FEATURE_ASK,), ("transcript", "unknown")])
def test_no_feature_set_accepts_it_as_a_command(features):
    assert p.LISTENING not in p.accepted_commands(features)


def test_it_is_not_one_of_the_other_frame_types():
    others = {p.HELLO, p.WELCOME, p.BYE, p.PING, p.PONG, p.ASK_ARM, p.ASK_DISARM, p.ASK_STATE}
    assert p.LISTENING not in others


def test_the_ask_feature_accepts_exactly_what_it_did():
    assert p.accepted_commands((p.FEATURE_ASK,)) == frozenset({p.ASK_ARM, p.ASK_DISARM})
