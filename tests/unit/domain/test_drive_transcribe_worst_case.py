"""T-R19: the per-window reservation bounds a window's worst case (Δ106 hard month cap): audio input, the text input
and the per-window output cap taken from the agent, at the held batch price."""
import pytest

from src.agents.drive_transcriber_agent import AGENT_TYPE, DriveTranscriberAgent
from src.domain.agent import AgentConfig
from src.domain.billing import BATCH_DISCOUNT, _PRICING_PER_MILLION_TOKENS
from src.domain.drive_index_params import TRANSCRIBE_WINDOW_PROMPT_TOKENS
from src.domain.drive_transcript import WindowSpec
from src.domain.drive_transcript_plan import window_cost_usd

AUDIO_TOKENS_PER_MIN = 1_500    # ~25 tokens per second of audio


@pytest.mark.parametrize("seconds", [8, 60, 300, 1_800, 1_860, 2_760])
def test_reservation_covers_the_worst_case_window(seconds):
    agent = DriveTranscriberAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE, capabilities=[AGENT_TYPE]))
    req = agent.build_request(WindowSpec(index=0, start_s=0.0, duration_s=float(seconds)), prompt="p", key="k",
                              file_name="files/x", file_mime="audio/ogg")
    rates = _PRICING_PER_MILLION_TOKENS["gemini-3.8-flash"]
    input_tokens = AUDIO_TOKENS_PER_MIN * seconds / 60 + TRANSCRIBE_WINDOW_PROMPT_TOKENS
    worst = (input_tokens * rates["input"] + req.max_output_tokens * rates["output"]) / 1e6 * BATCH_DISCOUNT
    assert window_cost_usd(seconds) >= worst - 1e-12


def test_a_short_window_cap_has_the_thinking_floor():
    """T-R26: thinking tokens count toward the cap; an 8 s window used to get 134 and ended MAX_TOKENS, empty."""
    agent = DriveTranscriberAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE, capabilities=[AGENT_TYPE]))
    req = agent.build_request(WindowSpec(index=0, start_s=0.0, duration_s=8.0), prompt="p", key="k",
                              file_name="files/x", file_mime="audio/ogg")
    assert req.max_output_tokens == 4_096
