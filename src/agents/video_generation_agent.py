"""
VideoGenerationAgent — video generation & editing via grok-imagine-video-1.5.

DeepResearchAgent-shaped: ExecutionMode.SYNC. execute() does only the fast part
(craft prompt, submit to xAI, arrange delivery) and returns an ACK in seconds —
the multi-minute wait happens entirely outside this request, via
WorkerHandler._handle_video_generation_polling. See
docs/10_rfcs/VIDEO_GENERATION_RFC.md §3.3-3.4.

Duration/resolution are NOT crafted by the LLM (§3.11 decision #9) — they default
to 5s/480p and are overridden only by an explicit payload.duration/payload.resolution
that Smart passes when the user literally asked for a specific length/quality. The
resolved duration is then clamped to self._max_duration_s (§3.11 decision #10).
"""
import json
import mimetypes
from typing import Optional

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.llm import LLMRequest, LLMResponse, Message, MessagePart, describe_empty_output
from ..domain.retry_policy import NO_RETRY_POLICY
from ..infrastructure.agent_config import VIDEO_GENERATION
from ..infrastructure.agent_manifest import Intent
from ..ports.llm_port import AgentExecutionContext
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.video_generation_port import VideoGenerationPort
from ..utils.logger import logger
from .base_agent import BaseAgent

DEFAULT_VIDEO_DURATION_S = 5
DEFAULT_VIDEO_RESOLUTION = "480p"

# edit_video runs SYNC inside the user's live request (60s client timeout, 1 vCPU
# instance) and base64-inlines the whole video into a JSON POST body — a large
# video risks OOM/timeout before it ever reaches xAI. 20MB of raw video (~27MB
# after base64 inflation) is a reasonable starting cap for that path.
MAX_EDIT_VIDEO_BYTES = 20 * 1024 * 1024


class VideoGenerationAgent(BaseAgent):
    """Crafts an Aurora-video prompt, submits to xAI, returns an ACK."""

    RETRY_POLICY = NO_RETRY_POLICY  # a transient 5xx after xAI already billed a render must not retry

    TEMPERATURE = VIDEO_GENERATION.temperature
    MAX_TOKENS = VIDEO_GENERATION.max_tokens
    REQUEST_TIMEOUT_S = VIDEO_GENERATION.request_timeout_s

    _RESPONSE_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "video_prompt": {"type": "STRING"},
            "aspect_ratio": {"type": "STRING"},
        },
        "required": ["video_prompt", "aspect_ratio"],
    }

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        video_port: VideoGenerationPort,
        prompt_builder: PromptBuilderPort,
        user_id: Optional[str] = None,
        file_conversion=None,
        max_duration_s: int = 10,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._video_port = video_port
        self.prompt_builder = prompt_builder
        self.user_id = user_id
        self._file_conversion = file_conversion
        self._max_duration_s = max_duration_s

    async def can_handle(self, message: AgentMessage) -> bool:
        intent_name = message.payload.get("intent")
        if intent_name not in (Intent.GENERATE_VIDEO, Intent.EDIT_VIDEO):
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

        self._on_agent_start(query)

        intent_name = message.payload.get("intent")
        if intent_name == Intent.EDIT_VIDEO:
            # Validated before any LLM call — same "fail before the paid crafting
            # call" pattern as ImageGenerationAgent's image_refs check. video_ref
            # is a context_schemas-declared field — see _resolve_duration's comment
            # below for why it's read from message.payload, not message.context.
            if not message.payload.get("video_ref"):
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error=(
                        "video_ref is required for edit_video. Look for [File: name (size)] "
                        'in the conversation and pass the filename as context={"video_ref": "<filename>"}.'
                    ),
                )

        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                account_id=message.context.get("account_id"),
                agent_type="video_generation",
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

        # Tell the crafting LLM what it can't otherwise infer from a text-only brief:
        # whether an image is attached (image-to-video vs text-to-video) and whether
        # this is a surgical edit_video vs a creative generate_video — the *instruction*
        # for what to do with that lives in the prompt token, not here. Mirrors
        # ImageGenerationAgent's craft_query augmentation (execute(), reference-count
        # signal before crafting).
        craft_query = query
        if intent_name == Intent.EDIT_VIDEO:
            craft_query = (
                f"{query}\n\n[Note: this is a surgical edit_video request — stay "
                "precise, preserve everything not explicitly asked to change.]"
            )
        elif message.payload.get("image_ref"):
            craft_query = (
                f"{query}\n\n[Note: animating an attached starting image — describe "
                "motion for this specific image, not a new scene.]"
            )

        craft_response = await self._craft_prompt(system_prompt, craft_query)
        crafted = self._parse_crafted_response(craft_response)
        if crafted is None:
            reason = describe_empty_output(craft_response.finish_reason)
            err = ValueError(f"LLM returned invalid/empty crafting output: {reason}")
            self._on_agent_error(err, "prompt_crafting")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Could not craft a video prompt from the request — {reason}.",
            )

        video_prompt, aspect_ratio = crafted
        if intent_name == Intent.EDIT_VIDEO:
            return await self._execute_edit(message, video_prompt)
        return await self._execute_generate(message, video_prompt, aspect_ratio)

    async def _craft_prompt(self, system_prompt: str, query: str) -> LLMResponse:
        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_prompt,
            messages=[Message(role="user", parts=[MessagePart(text=query)])],
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
            response_mime_type="application/json",
            response_schema=self._RESPONSE_SCHEMA,
            timeout=self.REQUEST_TIMEOUT_S,
        )
        return await self._call_llm(request)

    def _parse_crafted_response(self, response: LLMResponse) -> Optional[tuple[str, str]]:
        raw = (response.text or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("VideoGenerationAgent: crafting call returned invalid JSON: %r", raw[:200])
            return None
        video_prompt = (data.get("video_prompt") or "").strip()
        aspect_ratio = (data.get("aspect_ratio") or "auto").strip()
        if not video_prompt:
            return None
        return video_prompt, aspect_ratio

    def _resolve_duration(self, message: AgentMessage) -> int:
        # context_schemas-declared field ("duration") — AgentCoordinator spreads the
        # LLM's delegate_to_specialist context={} argument into message.payload, NOT
        # message.context (message.context holds coordinator-level fields only —
        # session_id, account_id, etc. — see AgentCoordinator._execute_async/
        # _execute_sync's "params" handling). Same mechanism/reasoning as
        # ImageGenerationAgent._resolve_resolution; reading message.context here
        # would silently drop every explicit duration the orchestrator requests.
        requested = message.payload.get("duration")
        duration = int(requested) if requested else DEFAULT_VIDEO_DURATION_S
        return min(duration, self._max_duration_s)

    def _resolve_resolution(self, message: AgentMessage) -> str:
        # context_schemas-declared field — see _resolve_duration's comment.
        return message.payload.get("resolution") or DEFAULT_VIDEO_RESOLUTION

    async def _execute_generate(
        self, message: AgentMessage, prompt: str, aspect_ratio: str,
    ) -> AgentResponse:
        duration = self._resolve_duration(message)
        resolution = self._resolve_resolution(message)

        image_data = None
        image_mime_type = "image/png"
        # context_schemas-declared field — see _resolve_duration's comment.
        image_ref = message.payload.get("image_ref")
        if image_ref:
            user_id = message.context.get("user_id", "")
            try:
                image_data = await self._file_conversion.resolve_bytes(image_ref, user_id)
                image_mime_type = mimetypes.guess_type(image_ref)[0] or "image/png"
            except Exception as e:
                self._on_agent_error(e, f"resolve image_ref {image_ref}")
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error=f"Could not read reference image '{image_ref}': {type(e).__name__}.",
                )

        # For image-to-video, the source image's own proportions must govern the
        # output — the crafting LLM never sees the image (text-only brief), so any
        # aspect_ratio it picks is an unrelated guess. Live-verified 2026-08-24:
        # sending it anyway made xAI squish/distort the source photo to fit that
        # unrelated ratio instead of preserving it. Only forward aspect_ratio for
        # pure text-to-video, where it's the only signal available.
        effective_aspect_ratio = None if image_data is not None else aspect_ratio

        try:
            request_id = await self._video_port.create_video(
                prompt,
                message.context.get("user_id", ""),
                message.context.get("account_id", ""),
                image_data=image_data,
                image_mime_type=image_mime_type,
                duration=duration,
                resolution=resolution,
                aspect_ratio=effective_aspect_ratio,
                session_id=message.context.get("session_id"),
                origin_platform=message.context.get("origin_platform"),
            )
        except Exception as e:
            self._on_agent_error(e, "create_video")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Video generation failed to start: {type(e).__name__}.",
            )

        self._on_agent_success(len(prompt), 0)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={"status": "started", "request_id": request_id},
            confidence=1.0,
            metadata={"duration_s": duration, "resolution": resolution, "model": self.model_name},
        )

    async def _execute_edit(self, message: AgentMessage, prompt: str) -> AgentResponse:
        # context_schemas-declared field — see _resolve_duration's comment above.
        video_ref = message.payload.get("video_ref")
        # Genuine coordinator-level field (not context_schemas-declared) — stays
        # on message.context, same as everywhere else in this file.
        user_id = message.context.get("user_id", "")
        try:
            video_data = await self._file_conversion.resolve_bytes(video_ref, user_id)
        except Exception as e:
            self._on_agent_error(e, f"resolve video_ref {video_ref}")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Could not read reference video '{video_ref}': {type(e).__name__}.",
            )

        if len(video_data) > MAX_EDIT_VIDEO_BYTES:
            self._on_agent_error(
                ValueError(f"video too large: {len(video_data)} bytes"),
                f"resolve video_ref {video_ref}",
            )
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="Video too large to edit — please use a smaller clip.",
            )

        video_mime_type = mimetypes.guess_type(video_ref)[0] or "video/mp4"

        try:
            request_id = await self._video_port.edit_video(
                prompt, video_data,
                message.context.get("user_id", ""),
                message.context.get("account_id", ""),
                video_mime_type=video_mime_type,
                session_id=message.context.get("session_id"),
                origin_platform=message.context.get("origin_platform"),
            )
        except Exception as e:
            self._on_agent_error(e, "edit_video")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Video edit failed to start: {type(e).__name__}.",
            )

        self._on_agent_success(len(prompt), 0)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={"status": "started", "request_id": request_id},
            confidence=1.0,
            metadata={"model": self.model_name},
        )
