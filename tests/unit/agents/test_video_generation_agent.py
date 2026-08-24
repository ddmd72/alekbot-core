"""
Unit tests for VideoGenerationAgent.

Covers:
- can_handle: generate_video with query -> True; empty query, unknown intent -> False;
  edit_video with video_ref -> True
- execute generate_video: LLM call #1 crafts {video_prompt, aspect_ratio},
  port.create_video() called with resolved duration/resolution, ACK response
  (no delivery_items)
- No context.duration/resolution -> defaults to 5s/480p
- Explicit context.duration under the cap -> honored as-is
- Explicit context.duration over max_duration_s -> clamped
- Per-user max_duration_s override raises the effective ceiling
- generate_video with image_ref -> resolved via FileConversionService.resolve_bytes()
- Prompt builder failure -> AgentResponse.failure(), no silent fallback
- Malformed/empty structured JSON from crafting call -> failure, no create_video() call
- Port create_video() raises -> failure response
- execute edit_video: video_ref resolved via FileConversionService.resolve_bytes(),
  port.edit_video() called (never receives duration/resolution), ACK response
- edit_video missing video_ref -> failure before the (paid) crafting LLM call runs
- edit_video resolve_bytes()/port.edit_video() failures -> failure response

NOTE on context_schemas fields (duration/resolution/image_ref/video_ref): these are
delivered to the agent via AgentMessage.payload in production (AgentCoordinator
spreads the LLM's context={} tool-call argument into payload, NOT into
message.context — see AgentCoordinator._execute_sync/_execute_async's "params"
handling). Tests that need these fields set them directly on msg.payload[...]
after constructing the message via _make_message(), mirroring the pattern in
tests/unit/agents/test_image_generation_agent.py for resolution/quality/image_refs.
The _make_message() context= kwarg below is reserved for genuine
coordinator-level fields only (user_id, account_id, session_id).
"""
from unittest.mock import AsyncMock

import pytest

from src.agents.video_generation_agent import VideoGenerationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.video_generation_port import VideoGenerationPort
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.services.file_conversion_service import FileConversionService

_QUERY = "a cat chasing a laser pointer across a wooden floor"
_CRAFTED_JSON = '{"video_prompt": "A tabby cat pounces after a red laser dot across a sunlit wooden floor, playful energy, tracking shot", "aspect_ratio": "16:9"}'


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="video_generation",
        provider=mock_llm,
        model_name="grok-4.6-test",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
        resilience_port=InMemoryProviderResilience(),
    )


def _make_message(
    intent_name: str = "generate_video",
    query: str = _QUERY,
    context: dict | None = None,
) -> AgentMessage:
    # context= is for genuine coordinator-level fields only (user_id,
    # account_id, session_id) — NEVER for context_schemas-declared fields
    # (duration/resolution/image_ref), which reach the agent via
    # message.payload in production. Tests needing those set msg.payload[...]
    # directly after construction.
    ctx = {"user_id": "user123", "account_id": "acc1"}
    if context:
        ctx.update(context)
    return AgentMessage(
        intent=AgentIntent.DELEGATE,
        payload={"query": query, "intent": intent_name},
        sender="smart_response_agent",
        recipient="video_generation_agent",
        task_id="task_vid_1",
        context=ctx,
    )


@pytest.fixture
def mock_llm():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = LLMResponse(text=_CRAFTED_JSON, tool_calls=[])
    return m


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a video-generation prompt specialist..."
    return pb


@pytest.fixture
def mock_video_port():
    port = AsyncMock(spec=VideoGenerationPort)
    port.create_video.return_value = "req-abc123"
    return port


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_video_port):
    config = AgentConfig(
        agent_id="video_generation_agent_user123",
        agent_type="video_generation",
    )
    return VideoGenerationAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        max_duration_s=10,
    )


# ============================================================================
# can_handle
# ============================================================================

async def test_can_handle_generate_video(agent):
    assert await agent.can_handle(_make_message("generate_video")) is True


async def test_can_handle_empty_query(agent):
    assert await agent.can_handle(_make_message("generate_video", query="")) is False


async def test_can_handle_unknown_intent(agent):
    assert await agent.can_handle(_make_message("some_other_intent")) is False


async def test_can_handle_edit_video_with_video_ref(agent):
    msg = _make_message("edit_video")
    msg.payload["video_ref"] = "clip.mp4"
    assert await agent.can_handle(msg) is True


# ============================================================================
# execute — origin_platform forwarding (delivery-channel bug, live-verified 2026-08-24)
# ============================================================================

async def test_execute_generate_video_forwards_origin_platform(agent, mock_video_port):
    msg = _make_message("generate_video", context={"origin_platform": "slack"})

    await agent.execute(msg)

    assert mock_video_port.create_video.call_args.kwargs["origin_platform"] == "slack"


async def test_execute_edit_video_forwards_origin_platform(
    mock_llm, mock_prompt_builder, mock_video_port,
):
    from src.services.file_conversion_service import FileConversionService
    mock_file_conversion = AsyncMock(spec=FileConversionService)
    mock_file_conversion.resolve_bytes.return_value = b"source-video-bytes"
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
        max_duration_s=10,
    )
    msg = _make_message("edit_video", context={"origin_platform": "telegram"})
    msg.payload["video_ref"] = "clip.mp4"

    await agent.execute(msg)

    assert mock_video_port.edit_video.call_args.kwargs["origin_platform"] == "telegram"


# ============================================================================
# execute — generate_video, default/clamp logic
# ============================================================================

async def test_execute_generate_video_happy_path_defaults(agent, mock_video_port):
    response = await agent.execute(_make_message("generate_video"))

    assert response.status == AgentStatus.SUCCESS
    assert response.delivery_items == []
    mock_video_port.create_video.assert_awaited_once()
    call_kwargs = mock_video_port.create_video.call_args.kwargs
    assert call_kwargs["duration"] == 5
    assert call_kwargs["resolution"] == "480p"
    assert call_kwargs["aspect_ratio"] == "16:9"
    assert response.result == {"status": "started", "request_id": "req-abc123"}


async def test_execute_generate_video_explicit_duration_under_cap_is_honored(agent, mock_video_port):
    msg = _make_message("generate_video")
    msg.payload["duration"] = 8
    await agent.execute(msg)

    assert mock_video_port.create_video.call_args.kwargs["duration"] == 8


async def test_execute_generate_video_explicit_duration_over_cap_is_clamped(agent, mock_video_port):
    msg = _make_message("generate_video")
    msg.payload["duration"] = 14
    await agent.execute(msg)

    # agent fixture's max_duration_s=10
    assert mock_video_port.create_video.call_args.kwargs["duration"] == 10


async def test_execute_generate_video_per_user_override_raises_ceiling(mock_llm, mock_prompt_builder, mock_video_port):
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        max_duration_s=20,  # this user's own override, higher than the system default of 10
    )

    msg = _make_message("generate_video")
    msg.payload["duration"] = 14
    await agent.execute(msg)

    assert mock_video_port.create_video.call_args.kwargs["duration"] == 14


async def test_execute_generate_video_explicit_resolution_is_honored(agent, mock_video_port):
    msg = _make_message("generate_video")
    msg.payload["resolution"] = "1080p"
    await agent.execute(msg)

    assert mock_video_port.create_video.call_args.kwargs["resolution"] == "1080p"


# ============================================================================
# execute — generate_video, image_ref
# ============================================================================

async def test_execute_generate_video_with_image_ref_resolves_bytes(mock_llm, mock_prompt_builder, mock_video_port):
    mock_file_conversion = AsyncMock(spec=FileConversionService)
    mock_file_conversion.resolve_bytes.return_value = b"source-image-bytes"
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
        max_duration_s=10,
    )

    msg = _make_message("generate_video")
    msg.payload["image_ref"] = "photo.jpg"
    await agent.execute(msg)

    mock_file_conversion.resolve_bytes.assert_awaited_once_with("photo.jpg", "user123")
    call_kwargs = mock_video_port.create_video.call_args.kwargs
    assert call_kwargs["image_data"] == b"source-image-bytes"
    assert call_kwargs["image_mime_type"] == "image/jpeg"
    # Real bug, live-verified 2026-08-24: xAI squished the source photo to fit the
    # crafting LLM's guessed aspect_ratio ("16:9" per _CRAFTED_JSON) instead of
    # preserving the source image's own proportions. For image-to-video the crafted
    # aspect_ratio must be discarded, not forwarded to the port.
    assert call_kwargs["aspect_ratio"] is None


# ============================================================================
# execute — crafting-call context markers (Fix 3: crafting LLM never told an
# image is present, or that it's edit_video — RFC §3.3 "brief (+ image presence)
# -> {video_prompt, aspect_ratio}")
# ============================================================================

def _crafted_query_text(mock_llm) -> str:
    crafting_request = mock_llm.generate_content.call_args.kwargs["request"]
    return crafting_request.messages[0].parts[0].text


async def test_crafting_call_has_no_marker_on_plain_generate_video(agent, mock_llm):
    await agent.execute(_make_message("generate_video"))

    assert "[Note:" not in _crafted_query_text(mock_llm)


async def test_crafting_call_includes_image_presence_marker_with_image_ref(
    mock_llm, mock_prompt_builder, mock_video_port,
):
    mock_file_conversion = AsyncMock(spec=FileConversionService)
    mock_file_conversion.resolve_bytes.return_value = b"source-image-bytes"
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
        max_duration_s=10,
    )

    msg = _make_message("generate_video")
    msg.payload["image_ref"] = "photo.jpg"
    await agent.execute(msg)

    text = _crafted_query_text(mock_llm)
    assert "animating an attached starting image" in text
    assert text.startswith(_QUERY)


async def test_crafting_call_includes_edit_mode_marker_for_edit_video(
    mock_llm, mock_prompt_builder, mock_video_port,
):
    mock_file_conversion = AsyncMock(spec=FileConversionService)
    mock_file_conversion.resolve_bytes.return_value = b"source-video-bytes"
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
        max_duration_s=10,
    )

    msg = _make_message("edit_video", query="make the sky sunset orange")
    msg.payload["video_ref"] = "clip.mp4"
    await agent.execute(msg)

    text = _crafted_query_text(mock_llm)
    assert "surgical edit_video request" in text
    assert text.startswith("make the sky sunset orange")


# ============================================================================
# execute — failure paths
# ============================================================================

async def test_execute_empty_query_fails(agent):
    response = await agent.execute(_make_message("generate_video", query=""))
    assert response.status == AgentStatus.FAILED


async def test_execute_prompt_builder_failure(agent, mock_prompt_builder):
    mock_prompt_builder.build_for_agent.side_effect = RuntimeError("Firestore unavailable")

    response = await agent.execute(_make_message("generate_video"))

    assert response.status == AgentStatus.FAILED
    assert "Firestore unavailable" in response.error


async def test_execute_malformed_json_from_crafting_call_fails(agent, mock_llm, mock_video_port):
    mock_llm.generate_content.return_value = LLMResponse(text="not valid json{{{", tool_calls=[])

    response = await agent.execute(_make_message("generate_video"))

    assert response.status == AgentStatus.FAILED
    mock_video_port.create_video.assert_not_awaited()


async def test_execute_empty_crafting_output_fails(agent, mock_llm, mock_video_port):
    mock_llm.generate_content.return_value = LLMResponse(text="", tool_calls=[])

    response = await agent.execute(_make_message("generate_video"))

    assert response.status == AgentStatus.FAILED
    mock_video_port.create_video.assert_not_awaited()


async def test_execute_port_create_video_raises_fails(agent, mock_video_port):
    mock_video_port.create_video.side_effect = RuntimeError("xAI 503")

    response = await agent.execute(_make_message("generate_video"))

    assert response.status == AgentStatus.FAILED


# ============================================================================
# execute — edit_video
# ============================================================================

async def test_execute_edit_video_happy_path(mock_llm, mock_prompt_builder, mock_video_port):
    mock_video_port.edit_video.return_value = "req-edit1"
    mock_file_conversion = AsyncMock(spec=FileConversionService)
    mock_file_conversion.resolve_bytes.return_value = b"source-video-bytes"
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
        max_duration_s=10,
    )

    msg = _make_message("edit_video", query="make the sky sunset orange")
    msg.payload["video_ref"] = "clip.mp4"
    response = await agent.execute(msg)

    assert response.status == AgentStatus.SUCCESS
    mock_file_conversion.resolve_bytes.assert_awaited_once_with("clip.mp4", "user123")
    call_kwargs = mock_video_port.edit_video.call_args.kwargs
    assert call_kwargs["video_mime_type"] == "video/mp4"
    assert response.result == {"status": "started", "request_id": "req-edit1"}
    # edit_video's port call must never receive duration/resolution — the port
    # method has no such parameters (editing preserves the source's own length).
    assert "duration" not in call_kwargs
    assert "resolution" not in call_kwargs


async def test_execute_edit_video_missing_video_ref_fails(agent):
    response = await agent.execute(_make_message("edit_video"))

    assert response.status == AgentStatus.FAILED
    assert "video_ref" in response.error


async def test_execute_edit_video_resolve_bytes_failure(mock_llm, mock_prompt_builder, mock_video_port):
    mock_file_conversion = AsyncMock(spec=FileConversionService)
    mock_file_conversion.resolve_bytes.side_effect = FileNotFoundError("no such file")
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
        max_duration_s=10,
    )

    msg = _make_message("edit_video")
    msg.payload["video_ref"] = "missing.mp4"
    response = await agent.execute(msg)

    assert response.status == AgentStatus.FAILED
    mock_video_port.edit_video.assert_not_awaited()


async def test_execute_edit_video_oversized_video_fails_without_calling_port(
    mock_llm, mock_prompt_builder, mock_video_port,
):
    from src.agents.video_generation_agent import MAX_EDIT_VIDEO_BYTES

    mock_file_conversion = AsyncMock(spec=FileConversionService)
    mock_file_conversion.resolve_bytes.return_value = b"x" * (MAX_EDIT_VIDEO_BYTES + 1)
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
        max_duration_s=10,
    )

    msg = _make_message("edit_video")
    msg.payload["video_ref"] = "clip.mp4"
    response = await agent.execute(msg)

    assert response.status == AgentStatus.FAILED
    assert "too large" in response.error.lower()
    mock_video_port.edit_video.assert_not_awaited()


async def test_execute_edit_video_port_raises_fails(mock_llm, mock_prompt_builder, mock_video_port):
    mock_video_port.edit_video.side_effect = RuntimeError("xAI 503")
    mock_file_conversion = AsyncMock(spec=FileConversionService)
    mock_file_conversion.resolve_bytes.return_value = b"source-video-bytes"
    agent = VideoGenerationAgent(
        config=AgentConfig(agent_id="video_generation_agent_user123", agent_type="video_generation"),
        execution_context=_make_execution_context(mock_llm),
        video_port=mock_video_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
        max_duration_s=10,
    )

    msg = _make_message("edit_video")
    msg.payload["video_ref"] = "clip.mp4"
    response = await agent.execute(msg)

    assert response.status == AgentStatus.FAILED
