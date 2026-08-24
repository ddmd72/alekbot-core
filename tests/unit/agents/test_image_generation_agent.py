"""
Unit tests for ImageGenerationAgent.

Covers:
- can_handle: correct intents with required fields → True; wrong intent,
  empty query, edit_image without image_ref → False
- execute generate_image: LLM call #1 crafts the prompt, port.generate() renders,
  one DeliveryItem(type="document") with file_upload=False (link-unfurl alone
  renders the image; native upload would duplicate it)
- execute generate_image: empty LLM output → failure
- execute generate_image: empty query → failure
- execute generate_image: prompt_builder failure → failure
- execute generate_image: port returns [] → failure, no DeliveryItem
(edit_image tests are in Task 5's additions to this file)
"""
import base64
from unittest.mock import AsyncMock, call

import pytest

from src.agents.image_generation_agent import ImageGenerationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse
from src.domain.user import PerformanceTier
from src.ports.image_generation_port import GeneratedImage, ImageGenerationPort, ReferenceImage
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.services.file_conversion_service import FileConversionService

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
    # False: the document link already unfurls into an inline image (Slack/Telegram
    # detect the image content-type) — a native upload would duplicate the same
    # picture a second time in the channel. Confirmed live in production 2026-08-21.
    assert item.data["file_upload"] is False
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


async def test_execute_generate_image_default_resolution_and_quality(agent, mock_image_port):
    await agent.execute(_make_message("generate_image"))

    call_kwargs = mock_image_port.generate.call_args.kwargs
    assert call_kwargs["resolution"] == "1k"
    assert call_kwargs["quality"] == "medium"


async def test_execute_generate_image_explicit_resolution_is_honored(agent, mock_image_port):
    # AgentCoordinator spreads the LLM's context={} argument into
    # message.payload, not message.context — see _resolve_resolution's
    # docstring. Same pattern already used for image_refs in this file.
    msg = _make_message("generate_image")
    msg.payload["resolution"] = "2k"

    await agent.execute(msg)

    assert mock_image_port.generate.call_args.kwargs["resolution"] == "2k"


async def test_execute_generate_image_explicit_quality_is_honored(agent, mock_image_port):
    msg = _make_message("generate_image")
    msg.payload["quality"] = "low"

    await agent.execute(msg)

    assert mock_image_port.generate.call_args.kwargs["quality"] == "low"


async def test_execute_generate_image_records_cost_via_quota_service(agent):
    # Default resolution/quality (1k/medium) -> $0.06 tier, not a flat $0.04 —
    # corrected 2026-08-24 after a live API probe proved quality changes price.
    agent._quota_service = AsyncMock()

    await agent.execute(_make_message("generate_image"))

    agent._quota_service.record_usage.assert_awaited_once_with(
        account_id="acc1", model="grok-imagine-image-2.0", tokens=0, cost=0.06,
    )


async def test_execute_generate_image_records_cost_for_explicit_tier(agent):
    agent._quota_service = AsyncMock()
    msg = _make_message("generate_image")
    msg.payload["resolution"] = "2k"
    msg.payload["quality"] = "low"

    await agent.execute(msg)

    agent._quota_service.record_usage.assert_awaited_once_with(
        account_id="acc1", model="grok-imagine-image-2.0", tokens=0, cost=0.06,
    )


async def test_execute_generate_image_skips_billing_when_quota_service_unset(agent):
    # agent._quota_service defaults to None (BaseAgent.__init__) in this fixture,
    # same as every unit test constructing the agent directly (not via
    # UserAgentFactory) — must not raise.
    response = await agent.execute(_make_message("generate_image"))
    assert response.status == AgentStatus.SUCCESS


# ============================================================================
# Fixtures for edit_image tests
# ============================================================================

@pytest.fixture
def mock_file_conversion():
    fc = AsyncMock(spec=FileConversionService)
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
        context={"image_refs": ["photo.jpg"]},
    )
    # image_refs is spread into payload by the coordinator in production (context_schemas
    # mechanism) — simulate that here since this test bypasses the coordinator.
    msg.payload["image_refs"] = ["photo.jpg"]

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.SUCCESS
    mock_file_conversion.resolve_bytes.assert_called_once_with("photo.jpg", "user123")
    mock_image_port.edit.assert_called_once()
    edit_kwargs = mock_image_port.edit.call_args.kwargs
    assert edit_kwargs["reference_images"] == [
        ReferenceImage(data=b"original-photo-bytes", mime_type="image/jpeg")
    ]
    assert base64.b64decode(response.delivery_items[0].data["content_b64"]) == b"edited-bytes"


async def test_execute_edit_image_derives_mime_type_from_image_ref(agent_with_files, mock_image_port):
    """Fix 2 regression guard: each reference's mime_type must be derived from its
    filename's extension (mirrors FileManagementAgent._fetch's mimetypes.guess_type
    pattern), not hardcoded to image/png — a JPEG reference must be sent to the port
    as image/jpeg."""
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/jpeg")
    msg = _make_message(
        "edit_image",
        query="remove the person in the background",
        context={"image_refs": ["photo.jpg"]},
    )
    msg.payload["image_refs"] = ["photo.jpg"]

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.SUCCESS
    edit_kwargs = mock_image_port.edit.call_args.kwargs
    assert edit_kwargs["reference_images"][0].mime_type == "image/jpeg"


async def test_execute_edit_image_missing_image_ref_fails(agent_with_files):
    msg = _make_message("edit_image", query="remove the person")
    # No image_refs in payload at all.

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED
    assert "image_ref" in response.error


async def test_execute_edit_image_never_reads_file_content(agent_with_files, mock_image_port):
    """
    Regression guard for RFC §3.4: edit_image must resolve image_refs itself via
    FileConversionService.resolve_bytes(), NEVER via a payload["file_content"]
    field (which is what AgentCoordinator._resolve_file_refs() would inject if
    this intent had used the key "file_ref" instead of "image_refs" — and which
    would be garbage/alert text for a binary image, not usable pixels).
    """
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/png")
    msg = _make_message("edit_image", query="remove the person", context={"image_refs": ["photo.jpg"]})
    msg.payload["image_refs"] = ["photo.jpg"]
    # Simulate what _resolve_file_refs would have injected had the key been "file_ref" —
    # a text alert, not usable image bytes. The agent must not touch this field at all.
    msg.payload["file_content"] = "[System: could not convert binary image to text]"

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.SUCCESS
    edit_kwargs = mock_image_port.edit.call_args.kwargs
    assert edit_kwargs["reference_images"] == [
        ReferenceImage(data=b"original-photo-bytes", mime_type="image/jpeg")
    ]  # from resolve_bytes, not file_content


async def test_execute_edit_image_resolve_bytes_failure(agent_with_files, mock_file_conversion, mock_image_port):
    mock_file_conversion.resolve_bytes.side_effect = FileNotFoundError("gone")
    msg = _make_message("edit_image", query="remove the person", context={"image_refs": ["photo.jpg"]})
    msg.payload["image_refs"] = ["photo.jpg"]

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED
    assert "photo.jpg" in response.error
    mock_file_conversion.resolve_bytes.assert_called_once_with("photo.jpg", "user123")
    mock_image_port.edit.assert_not_called()


async def test_execute_edit_image_port_failure(agent_with_files, mock_image_port):
    mock_image_port.edit.side_effect = RuntimeError("xAI edit failed")
    msg = _make_message("edit_image", query="remove the person", context={"image_refs": ["photo.jpg"]})
    msg.payload["image_refs"] = ["photo.jpg"]

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED
    mock_image_port.edit.assert_called_once()


# ============================================================================
# execute — edit_image with multiple reference images
# ============================================================================

async def test_execute_edit_image_multi_reference_happy_path(
    agent_with_files, mock_image_port, mock_file_conversion, mock_llm,
):
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/png")
    mock_file_conversion.resolve_bytes.side_effect = [b"bytes-a", b"bytes-b", b"bytes-c"]
    msg = _make_message(
        "edit_image",
        query="combine the lighting from the first photo with the subject from the second",
        context={"image_refs": ["a.jpg", "b.png", "c.jpg"]},
    )
    msg.payload["image_refs"] = ["a.jpg", "b.png", "c.jpg"]

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.SUCCESS
    assert mock_file_conversion.resolve_bytes.call_args_list == [
        call("a.jpg", "user123"), call("b.png", "user123"), call("c.jpg", "user123"),
    ]
    edit_kwargs = mock_image_port.edit.call_args.kwargs
    # Order must match image_refs order — xAI addresses references positionally
    # (<IMAGE_0>, <IMAGE_1>, <IMAGE_2>) and a reindex would desync the prompt from
    # what's actually sent.
    assert edit_kwargs["reference_images"] == [
        ReferenceImage(data=b"bytes-a", mime_type="image/jpeg"),
        ReferenceImage(data=b"bytes-b", mime_type="image/png"),
        ReferenceImage(data=b"bytes-c", mime_type="image/jpeg"),
    ]
    # The crafting LLM call must have been told the count + placeholder tokens.
    craft_request = mock_llm.generate_content.call_args.kwargs["request"]
    craft_text = craft_request.messages[0].parts[0].text
    assert "<IMAGE_0>" in craft_text and "<IMAGE_1>" in craft_text and "<IMAGE_2>" in craft_text
    assert "3 reference images" in craft_text


async def test_execute_edit_image_too_many_refs_fails(agent_with_files, mock_image_port, mock_llm):
    msg = _make_message(
        "edit_image",
        query="merge these",
        context={"image_refs": ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]},
    )
    msg.payload["image_refs"] = ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED
    assert "3" in response.error
    # Must fail before ever paying for the prompt-crafting LLM call.
    mock_llm.generate_content.assert_not_called()
    mock_image_port.edit.assert_not_called()


async def test_execute_edit_image_empty_refs_list_fails(agent_with_files, mock_image_port, mock_llm):
    msg = _make_message("edit_image", query="remove the person", context={"image_refs": []})
    msg.payload["image_refs"] = []

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED
    assert "image_refs" in response.error
    mock_llm.generate_content.assert_not_called()
    mock_image_port.edit.assert_not_called()


async def test_execute_edit_image_partial_resolve_failure(
    agent_with_files, mock_file_conversion, mock_image_port,
):
    mock_file_conversion.resolve_bytes.side_effect = [b"bytes-a", FileNotFoundError("gone")]
    msg = _make_message(
        "edit_image", query="merge these", context={"image_refs": ["a.jpg", "b.jpg"]},
    )
    msg.payload["image_refs"] = ["a.jpg", "b.jpg"]

    response = await agent_with_files.execute(msg)

    assert response.status == AgentStatus.FAILED
    assert "b.jpg" in response.error
    # No partial-success shape — the port must never be called on a partial set.
    mock_image_port.edit.assert_not_called()


async def test_execute_edit_image_records_higher_cost_than_generate(
    agent_with_files, mock_image_port, mock_file_conversion,
):
    mock_image_port.edit.return_value = GeneratedImage(data=b"\x89PNGfakebytes", mime_type="image/png")
    agent_with_files._quota_service = AsyncMock()
    msg = _make_message("edit_image", context={"image_refs": ["photo.png"]})
    # image_refs is spread into payload by the coordinator in production (context_schemas
    # mechanism) — simulate that here since this test bypasses the coordinator (same
    # pattern as every other edit_image test above).
    msg.payload["image_refs"] = ["photo.png"]

    await agent_with_files.execute(msg)

    # Default (1k/medium) tier $0.06 + $0.01 input-image surcharge = $0.07 — not
    # a flat $0.08 estimate. Corrected 2026-08-24 after live-verifying xAI's
    # actual pricing catalog (tiered by resolution/quality + a real surcharge).
    agent_with_files._quota_service.record_usage.assert_awaited_once_with(
        account_id="acc1", model="grok-imagine-image-2.0-edit", tokens=0, cost=0.07,
    )


async def test_execute_edit_image_passes_resolution_and_quality_to_port(
    agent_with_files, mock_image_port, mock_file_conversion,
):
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/png")
    msg = _make_message("edit_image")
    msg.payload["image_refs"] = ["photo.png"]
    msg.payload["resolution"] = "2k"
    msg.payload["quality"] = "low"

    await agent_with_files.execute(msg)

    edit_kwargs = mock_image_port.edit.call_args.kwargs
    assert edit_kwargs["resolution"] == "2k"
    assert edit_kwargs["quality"] == "low"


async def test_execute_edit_image_default_resolution_and_quality_to_port(
    agent_with_files, mock_image_port, mock_file_conversion,
):
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/png")
    msg = _make_message("edit_image", context={"image_refs": ["photo.png"]})
    msg.payload["image_refs"] = ["photo.png"]

    await agent_with_files.execute(msg)

    edit_kwargs = mock_image_port.edit.call_args.kwargs
    assert edit_kwargs["resolution"] == "1k"
    assert edit_kwargs["quality"] == "medium"


async def test_execute_edit_image_records_cost_for_explicit_tier(
    agent_with_files, mock_image_port, mock_file_conversion,
):
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/png")
    agent_with_files._quota_service = AsyncMock()
    msg = _make_message("edit_image")
    msg.payload["image_refs"] = ["photo.png"]
    msg.payload["resolution"] = "2k"
    msg.payload["quality"] = "medium"

    await agent_with_files.execute(msg)

    # (2k, medium) tier $0.08 + $0.01 input surcharge = $0.09.
    agent_with_files._quota_service.record_usage.assert_awaited_once_with(
        account_id="acc1", model="grok-imagine-image-2.0-edit", tokens=0, cost=0.09,
    )


async def test_execute_edit_image_single_ref_no_count_signal(agent_with_files, mock_llm, mock_image_port):
    """At count == 1, craft_query must be byte-identical to the raw query — no
    IMAGE_n placeholder text injected — preserving today's live single-image
    behavior exactly."""
    mock_image_port.edit.return_value = GeneratedImage(data=b"edited-bytes", mime_type="image/png")
    query = "remove the person in the background"
    msg = _make_message("edit_image", query=query, context={"image_refs": ["photo.jpg"]})
    msg.payload["image_refs"] = ["photo.jpg"]

    await agent_with_files.execute(msg)

    craft_request = mock_llm.generate_content.call_args.kwargs["request"]
    assert craft_request.messages[0].parts[0].text == query
