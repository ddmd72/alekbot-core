from datetime import datetime, timezone

from src.domain.voice_call_buffer import VoiceCallBuffer, VoiceTurnSegment

_T = datetime.now(timezone.utc)


def test_recent_exchanges_are_the_last_turns_in_order_without_empty_sides():
    buffer = VoiceCallBuffer(call_id="c")
    for i in range(4):
        buffer.add_turn(VoiceTurnSegment(request_text=f"q{i}", response_text="" if i == 3 else f"a{i}",
                                         started_at=_T, ended_at=_T))
    assert buffer.recent_exchanges(2) == [
        {"role": "user", "text": "q2"}, {"role": "lelik", "text": "a2"}, {"role": "user", "text": "q3"},
    ]
