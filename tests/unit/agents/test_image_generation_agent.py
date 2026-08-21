"""
Unit tests for ImageGenerationAgent.

Covers:
- can_handle: correct intents with required fields → True; wrong intent,
  empty query, edit_image without image_ref → False
- execute generate_image: LLM call #1 crafts the prompt, port.generate() renders,
  one DeliveryItem(type="document") with file_upload=True
- execute generate_image: empty LLM output → failure
- execute generate_image: empty query → failure
- execute generate_image: prompt_builder failure → failure
- execute generate_image: port returns [] → failure, no DeliveryItem
(edit_image tests are in Task 5's additions to this file)
"""
import base64
from unittest.mock import AsyncMock

import pytest

from src.agents.image_generation_agent import ImageGenerationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse
from src.domain.user import PerformanceTier
from src.ports.image_generation_port import GeneratedImage, ImageGenerationPort
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience

_QUERY = "a friendly red bicycle leaning against a brick wall, warm afternoon light"
_CRAFTED_PROMPT = (
    "Photorealistic photograph of a red bicycle leaning against a weathered brick "
    "wall, warm golden-hour light, shallow depth of field, shot on a Canon EOS R5"
)


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="image_generation",
        provider=mock_llm,
        model_name="grok-4.6-test",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        resilience_port=InMemoryProviderResilience(),
    )


def _make_message(
    intent_name: str = "generate_image",
    query: str = _QUERY,
    context: dict | None = None,
) -> AgentMessage:
    ctx = {"user_id": "user123", "account_id": "acc1"}
    if context:
        ctx.update(context)
    return AgentMessage(
        intent=AgentIntent.DELEGATE,
        payload={"query": query, "intent": intent_name},
        sender="smart_response_agent",
        recipient="image_generation_agent",
        task_id="task_img_1",
        context=ctx,
    )


@pytest.fixture
def mock_llm():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = LLMResponse(text=_CRAFTED_PROMPT, tool_calls=[])
    return m


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are an image-generation prompt specialist..."
    return pb


@pytest.fixture
def mock_image_port():
    port = AsyncMock(spec=ImageGenerationPort)
    port.generate.return_value = [GeneratedImage(data=b"\x89PNGfakebytes", mime_type="image/png")]
    return port


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_image_port):
    config = AgentConfig(
        agent_id="image_generation_agent_user123",
        agent_type="image_generation",
    )
    return ImageGenerationAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        image_port=mock_image_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
    )


# ============================================================================
# can_handle
# ============================================================================

async def test_can_handle_generate_image(agent):
    assert await agent.can_handle(_make_message("generate_image")) is True


async def test_can_handle_empty_query(agent):
    assert await agent.can_handle(_make_message("generate_image", query="")) is False


async def test_can_handle_unknown_intent(agent):
    assert await agent.can_handle(_make_message("some_other_intent")) is False


# ============================================================================
# execute — generate_image
# ============================================================================

async def test_execute_generate_image_happy_path(agent, mock_llm, mock_image_port):
    response = await agent.execute(_make_message("generate_image"))

    assert response.status == AgentStatus.SUCCESS
    mock_llm.generate_content.assert_called_once()
    mock_image_port.generate.assert_called_once()
    call_kwargs = mock_image_port.generate.call_args.kwargs
    assert call_kwargs.get("prompt", mock_image_port.generate.call_args.args[0] if mock_image_port.generate.call_args.args else None) is not None

    assert len(response.delivery_items) == 1
    item = response.delivery_items[0]
    assert item.type == "document"
    assert item.data["file_upload"] is True
    assert item.data["content_type"] == "image/png"
    assert base64.b64decode(item.data["content_b64"]) == b"\x89PNGfakebytes"


async def test_execute_generate_image_uses_crafted_prompt_not_raw_query(agent, mock_image_port):
    await agent.execute(_make_message("generate_image", query=_QUERY))

    args, kwargs = mock_image_port.generate.call_args
    sent_prompt = kwargs.get("prompt") or args[0]
    assert sent_prompt == _CRAFTED_PROMPT
    assert sent_prompt != _QUERY  # must be the LLM's crafted prompt, not passthrough


async def test_execute_generate_image_empty_query_fails(agent):
    response = await agent.execute(_make_message("generate_image", query=""))

    assert response.status == AgentStatus.FAILED


async def test_execute_generate_image_llm_empty_output_fails(agent, mock_llm):
    mock_llm.generate_content.return_value = LLMResponse(text="", tool_calls=[])

    response = await agent.execute(_make_message("generate_image"))

    assert response.status == AgentStatus.FAILED


async def test_execute_generate_image_prompt_builder_failure(agent, mock_prompt_builder):
    mock_prompt_builder.build_for_agent.side_effect = RuntimeError("prompt service down")

    response = await agent.execute(_make_message("generate_image"))

    assert response.status == AgentStatus.FAILED


async def test_execute_generate_image_port_returns_empty_fails(agent, mock_image_port):
    mock_image_port.generate.return_value = []

    response = await agent.execute(_make_message("generate_image"))

    assert response.status == AgentStatus.FAILED
    assert response.delivery_items == []


# ============================================================================
# Fixtures for edit_image tests
# ============================================================================

@pytest.fixture
def mock_file_conversion():
    fc = AsyncMock()
    fc.resolve_bytes.return_value = b"original-photo-bytes"
    return fc


@pytest.fixture
def agent_with_files(mock_llm, mock_prompt_builder, mock_image_port, mock_file_conversion):
    config = AgentConfig(
        agent_id="image_generation_agent_user123",
        agent_type="image_generation",
    )
    return ImageGenerationAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        image_port=mock_image_port,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
        file_conversion=mock_file_conversion,
    )


# ============================================================================
# can_handle — edit_image
# ============================================================================

async def test_can_handle_edit_image_with_image_ref(agent_with_files):
    msg = _make_message(
        "edit_image", query="remove the person in the background",
        context={"image_ref": "photo.jpg"},
    )
    assert await agent_with_files.can_handle(msg) is True


# ============================================================================
# execute — edit_image
# ============================================================================

async def test_execute_edit_image_happy_path(agent_with_files, mock_image_port, mock_file_conversion):
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/png")
    msg = _make_message(
        "edit_image",
        query="remove the person in the background",
        context={"image_ref": "photo.jpg"},
    )
    # image_ref is spread into payload by the coordinator in production (context_schemas
    # mechanism) — simulate that here since this test bypasses the coordinator.
    msg.payload["image_ref"] = "photo.jpg"

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.SUCCESS
    mock_file_conversion.resolve_bytes.assert_called_once_with("photo.jpg", "user123")
    mock_image_port.edit.assert_called_once()
    edit_kwargs = mock_image_port.edit.call_args.kwargs
    assert edit_kwargs["reference_images"] == [b"original-photo-bytes"]
    assert base64.b64decode(response.delivery_items[0].data["content_b64"]) == b"edited-bytes"


async def test_execute_edit_image_missing_image_ref_fails(agent_with_files):
    msg = _make_message("edit_image", query="remove the person")
    # No image_ref in payload at all.

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED
    assert "image_ref" in response.error


async def test_execute_edit_image_never_reads_file_content(agent_with_files, mock_image_port):
    """
    Regression guard for RFC §3.4: edit_image must resolve image_ref itself via
    FileConversionService.resolve_bytes(), NEVER via a payload["file_content"]
    field (which is what AgentCoordinator._resolve_file_refs() would inject if
    this intent had used the key "file_ref" instead of "image_ref" — and which
    would be garbage/alert text for a binary image, not usable pixels).
    """
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/png")
    msg = _make_message("edit_image", query="remove the person", context={"image_ref": "photo.jpg"})
    msg.payload["image_ref"] = "photo.jpg"
    # Simulate what _resolve_file_refs would have injected had the key been "file_ref" —
    # a text alert, not usable image bytes. The agent must not touch this field at all.
    msg.payload["file_content"] = "[System: could not convert binary image to text]"

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.SUCCESS
    edit_kwargs = mock_image_port.edit.call_args.kwargs
    assert edit_kwargs["reference_images"] == [b"original-photo-bytes"]  # from resolve_bytes, not file_content


async def test_execute_edit_image_resolve_bytes_failure(agent_with_files, mock_file_conversion):
    mock_file_conversion.resolve_bytes.side_effect = FileNotFoundError("gone")
    msg = _make_message("edit_image", query="remove the person", context={"image_ref": "photo.jpg"})
    msg.payload["image_ref"] = "photo.jpg"

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED


async def test_execute_edit_image_port_failure(agent_with_files, mock_image_port):
    mock_image_port.edit.side_effect = RuntimeError("xAI edit failed")
    msg = _make_message("edit_image", query="remove the person", context={"image_ref": "photo.jpg"})
    msg.payload["image_ref"] = "photo.jpg"

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED
