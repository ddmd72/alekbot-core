"""
ImageGenerationAgent
=====================

Specialist agent: receives a natural-language creative brief (generate_image) or
edit instruction (edit_image), crafts an Aurora-optimized prompt via one LLM call,
renders it via ImageGenerationPort (grok-imagine-image-2.0), and returns the
image as a "document" DeliveryItem (GCS link + native inline upload).

Registration: internal=False — exposed to LLMs via Intent.GENERATE_IMAGE / Intent.EDIT_IMAGE.
Dispatched ASYNC by AgentWorkerHandler via Cloud Tasks.

Why a real LLM call and not zero-LLM passthrough: grok-imagine-image-2.0 responds
to three distinct, non-obvious prompting techniques (photoreal scene anchoring,
exact-text-with-placement for dense layouts, style-lock for asset sets) that a
general orchestrator's conversational phrasing won't reliably produce. See
docs/10_rfcs/IMAGE_GENERATION_RFC.md §2.1.

payload["query"] is the raw natural-language brief/instruction from the orchestrator.
For edit_image, payload["image_ref"] (spread from context by the coordinator) names
the reference image file — resolved via FileConversionService.resolve_bytes(), NOT
via the coordinator's generic file_ref auto-injection (RFC §3.4).
"""
import base64
import time
from typing import List, Optional

from .base_agent import BaseAgent
from ..domain.retry_policy import NO_RETRY_POLICY
from ..domain.agent import AgentConfig, AgentMessage, AgentResponse, DeliveryItem
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import IMAGE_GENERATION
from ..infrastructure.agent_manifest import Intent
from ..ports.image_generation_port import ImageGenerationPort
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger


class ImageGenerationAgent(BaseAgent):
    """
    Specialist agent: crafts an Aurora-ready prompt, renders it, delivers the image.

    Accepts generate_image (payload["query"] = creative brief) and edit_image
    (payload["query"] = edit instruction, payload["image_ref"] = reference filename).
    Returns AgentResponse with one delivery_item on success: image "document"
    (GCS link + native inline upload).
    """

    # ASYNC image generation — no automatic retry (avoid double-billing xAI on retry).
    RETRY_POLICY = NO_RETRY_POLICY

    TEMPERATURE = IMAGE_GENERATION.temperature
    MAX_TOKENS = IMAGE_GENERATION.max_tokens
    THINKING_EFFORT = IMAGE_GENERATION.thinking_effort
    REQUEST_TIMEOUT_S = IMAGE_GENERATION.request_timeout_s

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        image_port: ImageGenerationPort,
        prompt_builder: PromptBuilderPort,
        user_id: Optional[str] = None,
        file_conversion=None,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._image_port = image_port
        self.prompt_builder = prompt_builder
        self.user_id = user_id
        self._file_conversion = file_conversion

    async def can_handle(self, message: AgentMessage) -> bool:
        intent_name = message.payload.get("intent")
        if intent_name not in (Intent.GENERATE_IMAGE, Intent.EDIT_IMAGE):
            return False
        return bool(message.payload.get("query"))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        if not query:
            self._on_agent_error(ValueError("No query provided"), "empty_query")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided in payload",
            )

        intent_name = message.payload.get("intent")
        self._on_agent_start(query)

        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                account_id=message.context.get("account_id"),
                agent_type="image_generation",
                user_id=self.user_id,
                include_biographical=False,
            )
        except Exception as exc:
            self._on_agent_error(exc, "prompt_builder")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Failed to build system prompt: {exc}",
            )

        crafted_prompt = await self._craft_prompt(system_prompt, query)
        if not crafted_prompt:
            err = ValueError("LLM returned empty prompt")
            self._on_agent_error(err, "prompt_crafting")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="Could not craft an image prompt from the request.",
            )

        if intent_name == Intent.EDIT_IMAGE:
            return await self._execute_edit(message, crafted_prompt)
        return await self._execute_generate(message, crafted_prompt)

    async def _craft_prompt(self, system_prompt: str, query: str) -> str:
        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_prompt,
            messages=[Message(role="user", parts=[MessagePart(text=query)])],
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
            thinking=self.THINKING_EFFORT or None,
            timeout=self.REQUEST_TIMEOUT_S,
        )
        response = await self._call_llm(request)
        return (response.text or "").strip()

    async def _execute_generate(self, message: AgentMessage, prompt: str) -> AgentResponse:
        start_time = time.time()
        images = await self._image_port.generate(prompt)
        return self._respond_with_images(message, images, start_time)

    async def _execute_edit(self, message: AgentMessage, prompt: str) -> AgentResponse:
        image_ref = message.payload.get("image_ref")
        if not image_ref:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(
                    "image_ref is required for edit_image. "
                    "Look for [File: name (size)] in the conversation and pass "
                    'the filename as context={"image_ref": "<filename>"}.'
                ),
            )

        start_time = time.time()
        try:
            reference_bytes = await self._file_conversion.resolve_bytes(
                image_ref, message.context.get("user_id", "")
            )
        except Exception as e:
            self._on_agent_error(e, f"resolve image_ref {image_ref}")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Could not read reference image '{image_ref}': {type(e).__name__}.",
            )

        try:
            image = await self._image_port.edit(prompt, reference_images=[reference_bytes])
        except Exception as e:
            self._on_agent_error(e, "image_edit")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Image edit failed: {type(e).__name__}.",
            )

        return self._respond_with_images(message, [image], start_time)

    def _respond_with_images(
        self, message: AgentMessage, images: List, start_time: float,
    ) -> AgentResponse:
        if not images:
            err = ValueError("Image port returned no images")
            self._on_agent_error(err, "image_generation")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No image was produced.",
            )

        image = images[0]
        duration_ms = int((time.time() - start_time) * 1000)
        ext = "png" if "png" in image.mime_type else "jpg"
        filename = f"image_{int(time.time())}.{ext}"

        self._on_agent_success(len(image.data), 0)
        logger.info(
            "ImageGenerationAgent: %d bytes duration=%dms", len(image.data), duration_ms,
        )

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result="image_generated",
            confidence=1.0,
            metadata={"duration_ms": duration_ms, "model": self.model_name},
            delivery_items=[
                DeliveryItem(
                    type="document",
                    data={
                        "content_b64": base64.b64encode(image.data).decode("utf-8"),
                        "filename": filename,
                        "content_type": image.mime_type,
                        "label": filename,
                        "file_upload": True,
                        "storage_class": message.context.get("storage_class", "document"),
                    },
                ),
            ],
        )
