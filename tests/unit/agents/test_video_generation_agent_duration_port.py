"""VideoGenerationAgent's edit_video source-duration check through a VideoDurationPort (S5b)."""
from typing import Optional
from unittest.mock import AsyncMock

import pytest

from src.agents.video_generation_agent import MAX_EDIT_VIDEO_SOURCE_DURATION_S, VideoGenerationAgent
from src.domain.agent import AgentConfig, AgentStatus
from src.ports.video_duration_port import VideoDurationPort
from src.services.file_conversion_service import FileConversionService
from tests.unit.agents.test_video_generation_agent import (  # noqa: F401  (fixtures)
    _make_execution_context,
    _make_message,
    mock_llm,
    mock_prompt_builder,
    mock_video_port,
)


class FixedDuration(VideoDurationPort):
    def __init__(self, value: Optional[float]) -> None:
        self.value = value

    async def duration_s(self, video: bytes) -> Optional[float]:
        return self.value


def _agent(mock_llm, mock_prompt_builder, mock_video_port, duration: Optional[float]) -> VideoGenerationAgent:
    files = AsyncMock(spec=FileConversionService)
    files.resolve_bytes.return_value = b"source-video-bytes"
    return VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm), video_port=mock_video_port,
        video_duration=FixedDuration(duration), prompt_builder=mock_prompt_builder, user_id="user123",
        file_conversion=files, max_duration_s=10)


async def _edit(agent):
    msg = _make_message("edit_video")
    msg.payload["video_ref"] = "clip.mp4"
    return await agent.execute(msg)


async def test_a_source_over_the_cap_is_refused(mock_llm, mock_prompt_builder, mock_video_port):
    agent = _agent(mock_llm, mock_prompt_builder, mock_video_port, MAX_EDIT_VIDEO_SOURCE_DURATION_S + 4.0)
    response = await _edit(agent)
    assert response.status == AgentStatus.FAILED and "shorter clip" in response.error
    mock_video_port.edit_video.assert_not_called()


@pytest.mark.parametrize("duration", [None, MAX_EDIT_VIDEO_SOURCE_DURATION_S - 3.0])
async def test_an_unknown_or_short_source_goes_to_the_video_port(mock_llm, mock_prompt_builder, mock_video_port,
                                                                 duration):
    agent = _agent(mock_llm, mock_prompt_builder, mock_video_port, duration)
    response = await _edit(agent)
    assert response.status == AgentStatus.SUCCESS
    mock_video_port.edit_video.assert_awaited_once()
