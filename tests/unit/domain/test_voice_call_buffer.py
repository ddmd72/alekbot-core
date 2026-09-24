from datetime import datetime, timezone
from src.domain.voice_call_buffer import VoiceCallBuffer, VoiceTurnSegment


def test_add_turn_appends_and_extends_transcript():
    buf = VoiceCallBuffer(call_id="c1", turns=[], usage_by_model={}, transcript_text="")
    seg = VoiceTurnSegment(
        request_text="hello",
        response_text="hi there",
        started_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 21, 0, 0, 2, tzinfo=timezone.utc),
    )
    buf.add_turn(seg)
    assert buf.turns == [seg]
    assert "hello" in buf.transcript_text and "hi there" in buf.transcript_text


def test_add_usage_accumulates_per_model():
    buf = VoiceCallBuffer(call_id="c1", turns=[], usage_by_model={}, transcript_text="")
    buf.add_usage("gpt-realtime-2.1", audio_input_tokens=100, audio_output_tokens=50)
    buf.add_usage("gpt-realtime-2.1", audio_input_tokens=20, text_output_tokens=5)
    assert buf.usage_by_model["gpt-realtime-2.1"] == {
        "audio_input_tokens": 120,
        "audio_output_tokens": 50,
        "text_output_tokens": 5,
    }
