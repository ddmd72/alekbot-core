"""transcription_cost for a model priced per token: the token legs win when the usage has them (and the cost is known)."""
import pytest

from src.domain import billing
from src.domain.audio_transcript import AudioTranscript


def test_a_token_priced_model_is_billed_on_its_token_legs_not_its_seconds(monkeypatch):
    monkeypatch.setitem(billing._TRANSCRIPTION_PRICING, "tokens-transcribe",
                        {"input": 2.0, "output": 10.0, "per_minute": 0.5})
    t = AudioTranscript(text="Lukas Weber owns the Q3 budget.", model="tokens-transcribe", seconds=60.0,
                        input_tokens=1_000, output_tokens=200)
    assert billing.transcription_cost_known(t)
    assert billing.transcription_cost(t) == pytest.approx((1_000 * 2.0 + 200 * 10.0) / 1_000_000)
