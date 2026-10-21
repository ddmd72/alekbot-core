"""T-R19: the per-window reservation bounds a window's worst case (Δ106 hard month cap): audio input, the text input
and the per-window output cap taken from the agent, at the held batch price."""
import pytest

from src.agents.drive_transcriber_agent import AGENT_TYPE, DriveTranscriberAgent
from src.domain.agent import AgentConfig
from src.domain.billing import BATCH_DISCOUNT, _PRICING_PER_MILLION_TOKENS
from src.domain.drive_index_params import TRANSCRIBE_WINDOW_PROMPT_TOKENS
from src.domain.drive_transcript import WindowSpec
from src.domain.drive_transcript_plan import estimate_usd, window_prompt_usd

AUDIO_TOKENS_PER_MIN = 1_500    # ~25 tokens per second of audio


@pytest.mark.parametrize("seconds", [60, 300, 1_800, 1_860, 2_760])
def test_reservation_covers_the_worst_case_window(seconds):
    agent = DriveTranscriberAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE, capabilities=[AGENT_TYPE]))
    req = agent.build_request(WindowSpec(index=0, start_s=0.0, duration_s=float(seconds)), prompt="p", key="k",
                              file_name="files/x", file_mime="audio/ogg")
    rates = _PRICING_PER_MILLION_TOKENS["gemini-3.8-flash"]
    input_tokens = AUDIO_TOKENS_PER_MIN * seconds / 60 + TRANSCRIBE_WINDOW_PROMPT_TOKENS
    worst = (input_tokens * rates["input"] + req.max_output_tokens * rates["output"]) / 1e6 * BATCH_DISCOUNT
    assert estimate_usd(seconds) + window_prompt_usd() >= worst
