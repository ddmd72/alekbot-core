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
For edit_image, payload["image_refs"] (spread from context by the coordinator, a list
of 1-3 filenames) names the reference image file(s) — resolved via
FileConversionService.resolve_bytes(), NOT via the coordinator's generic file_ref
auto-injection (RFC §3.4). When more than one reference is attached, the crafting
LLM call is told the count so it can address them as <IMAGE_0>, <IMAGE_1>, <IMAGE_2>
per xAI's convention — see docs/04_solution_strategy/decisions/image_edit_multi_reference.md.
"""
import asyncio
import base64
import mimetypes
import time
from typing import List, Optional

from .base_agent import BaseAgent
from ..domain.billing import IMAGE_EDIT_MODEL, IMAGE_GENERATE_MODEL, calculate_external_cost
from ..domain.retry_policy import NO_RETRY_POLICY
from ..domain.agent import AgentConfig, AgentMessage, AgentResponse, DeliveryItem
from ..domain.llm import LLMResponse, Message, MessagePart, describe_empty_output
from ..infrastructure.agent_config import IMAGE_GENERATION
from ..infrastructure.agent_manifest import Intent
from ..ports.image_generation_port import ImageGenerationPort, ReferenceImage
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger

_MAX_REFERENCE_IMAGES = 3


class ImageGenerationAgent(BaseAgent):
    """
    Specialist agent: crafts an Aurora-ready prompt, renders it, delivers the image.

    Accepts generate_image (payload["query"] = creative brief) and edit_image
    (payload["query"] = edit instruction, payload["image_refs"] = 1-3 reference
    filenames). Returns AgentResponse with one delivery_item on success: image
    "document" (GCS link + native inline upload).
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

        image_refs = message.payload.get("image_refs") or []
        if intent_name == Intent.EDIT_IMAGE:
            # Validated before any LLM call — an invalid ref count should never
            # pay for a prompt-crafting call it can't use.
            if not image_refs:
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error=(
                        "image_refs is required for edit_image. "
                        "Look for [File: name (size)] in the conversation and pass "
                        'the filename(s) as context={"image_refs": ["<filename1>", ...]}.'
                    ),
                )
            if len(image_refs) > _MAX_REFERENCE_IMAGES:
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error=(
                        f"edit_image supports at most {_MAX_REFERENCE_IMAGES} reference "
                        f"images, got {len(image_refs)}."
                    ),
                )

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

        craft_query = query
        if intent_name == Intent.EDIT_IMAGE and len(image_refs) > 1:
            # Tells the crafting LLM how many references it has and the exact
            # placeholder tokens to use — the *instruction* for what to do with
            # them lives in the COGNITIVE_PROCESS_IMAGE_GEN prompt token, not here.
            placeholders = ", ".join(f"<IMAGE_{i}>" for i in range(len(image_refs)))
            craft_query = (
                f"{query}\n\n[{len(image_refs)} reference images attached, "
                f"in order: {placeholders}]"
            )

        prompt_response = await self._craft_prompt(system_prompt, craft_query)
        crafted_prompt = (prompt_response.text or "").strip()
        if not crafted_prompt:
            reason = describe_empty_output(prompt_response.finish_reason)
            err = ValueError(f"LLM returned empty prompt: {reason}")
            self._on_agent_error(err, "prompt_crafting")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Could not craft an image prompt from the request — {reason}.",
            )

        if intent_name == Intent.EDIT_IMAGE:
            return await self._execute_edit(message, crafted_prompt, image_refs)
        return await self._execute_generate(message, crafted_prompt)

    async def _craft_prompt(self, system_prompt: str, query: str) -> LLMResponse:
        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_prompt,
            messages=[Message(role="user", parts=[MessagePart(text=query)])],
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
            thinking=self.THINKING_EFFORT or None,
            timeout=self.REQUEST_TIMEOUT_S,
        )
        return await self._call_llm(request)

    def _resolve_resolution(self, message: AgentMessage) -> str:
        return message.context.get("resolution") or "1k"

    def _resolve_quality(self, message: AgentMessage) -> str:
        return message.context.get("quality") or "medium"

    async def _execute_generate(self, message: AgentMessage, prompt: str) -> AgentResponse:
        start_time = time.time()
        images = await self._image_port.generate(
            prompt,
            resolution=self._resolve_resolution(message),
            quality=self._resolve_quality(message),
        )
        return await self._respond_with_images(message, images, start_time)

    async def _execute_edit(
        self, message: AgentMessage, prompt: str, image_refs: List[str],
    ) -> AgentResponse:
        user_id = message.context.get("user_id", "")
        start_time = time.time()

        # return_exceptions=True is required, not optional: the bare default lets
        # sibling resolve_bytes() tasks run unawaited/uncancelled after the first
        # exception. gather() preserves input order in its results regardless of
        # completion order, so the <IMAGE_n> <-> results[n] correspondence below
        # is safe as long as nothing reindexes after a partial failure — fail-fast
        # on any exception guarantees that.
        results = await asyncio.gather(
            *[self._file_conversion.resolve_bytes(ref, user_id) for ref in image_refs],
            return_exceptions=True,
        )

        failed = [
            (ref, result)
            for ref, result in zip(image_refs, results)
            if isinstance(result, Exception)
        ]
        if failed:
            failed_names = ", ".join(ref for ref, _ in failed)
            self._on_agent_error(failed[0][1], f"resolve image_refs {failed_names}")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(
                    f"Could not read reference image(s) '{failed_names}': "
                    f"{type(failed[0][1]).__name__}."
                ),
            )

        reference_images = [
            ReferenceImage(data=data, mime_type=mimetypes.guess_type(ref)[0] or "image/png")
            for ref, data in zip(image_refs, results)
        ]

        try:
            image = await self._image_port.edit(
                prompt,
                reference_images=reference_images,
                resolution=self._resolve_resolution(message),
                quality=self._resolve_quality(message),
            )
        except Exception as e:
            self._on_agent_error(e, "image_edit")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Image edit failed: {type(e).__name__}.",
            )

        return await self._respond_with_images(message, [image], start_time)

    async def _respond_with_images(
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

        # External-cost billing — see docs/10_rfcs/VIDEO_GENERATION_RFC.md §3.11
        # decision #11: this call previously did not exist at all, meaning xAI
        # image spend was invisible to the account's daily_cost_limit alert.
        # None-guarded because self._quota_service is None for every agent not
        # constructed through UserAgentFactory (BaseAgent.__init__ default);
        # awaited, not detached, per FirestoreQuotaService.record_usage's own
        # docstring (a fire-and-forget task past the request boundary is starved
        # by Cloud Run CPU throttling and lost on instance recycle).
        # resolution/quality re-resolved from the same message the execute path
        # already resolved them from (pure function of message.context, so this
        # is guaranteed consistent with whatever was actually sent to the port)
        # — the recorded cost must reflect the tier actually billed, not a
        # hardcoded average (see domain/billing.py's tiered pricing table).
        if self._quota_service:
            account_id = message.context.get("account_id", "")
            if account_id:
                intent_name = message.payload.get("intent")
                service_key = (
                    IMAGE_EDIT_MODEL
                    if intent_name == Intent.EDIT_IMAGE
                    else IMAGE_GENERATE_MODEL
                )
                cost = calculate_external_cost(
                    service_key,
                    resolution=self._resolve_resolution(message),
                    quality=self._resolve_quality(message),
                )
                await self._quota_service.record_usage(
                    account_id=account_id, model=service_key, tokens=0, cost=cost,
                )

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
                        # False on purpose: the document link already renders inline via
                        # the channel's own link-unfurl (image content-type), so a native
                        # file_upload here would just duplicate the same picture a second
                        # time in the channel. Confirmed live in production 2026-08-21 —
                        # unfurl alone is sufficient, and open_file re-read is driven by
                        # the GCS-backed link, not by this flag.
                        "file_upload": False,
                        "storage_class": message.context.get("storage_class", "document"),
                    },
                ),
            ],
        )
