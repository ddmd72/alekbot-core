"""
Platform-agnostic conversation handler.
Contains all business logic for processing messages from any platform.
"""
from __future__ import annotations

import base64
import os
import json
import asyncio
import time
import dataclasses
from typing import Callable, Coroutine, List, Optional, Any, TYPE_CHECKING

from ..domain.messaging import MessageContext, ResponseChannel, SmartResponse, RichContent
from ..domain.ui_messages import StatusType
from ..domain.agent import AgentMessage, AgentIntent, AgentStatus, DeliveryItem
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..infrastructure.agent_config import ENABLE_HISTORY_OPTIMIZATION
from ..ports.conversation_handler_port import ConversationHandlerPort
from ..services.localization_service import LocalizationService

if TYPE_CHECKING:
    from ..composition.user_agent_factory import UserAgentFactory
    from ..ports.file_service import FileService
    from ..ports.audio_transcription_port import AudioTranscriptionPort
    from ..services.file_conversion_service import FileConversionService
from ..utils.file_conversion import (
    convert_file_to_text, is_native_binary, make_history_stub,
)
from ..utils.logger import logger
from ..utils.telemetry import start_span
from ..utils.logging_context import set_log_context
from ..domain.settings import ConsolidationSettings
from ..services.rich_content_service import RichContentService
from ..services.user_notification_service import UserNotificationService
from ..services.agent_fallback_service import AgentFallbackService
from ..services.document_delivery_service import DocumentDeliveryService
from ..services.channel_binding_service import ChannelBindingService
from ..domain.channel_binding import ChannelBinding
from ..domain.session_mode import SessionMode

# Content types that require external fetch + platform upload (not Block Kit)
_MEDIA_CONTENT_TYPES = frozenset({"weather_image", "map_image", "file", "widget"})


def strtobool(val: str) -> bool:
    """
    Convert string to boolean (replacement for deprecated distutils.util.strtobool).
    
    Python 3.12+ removed distutils from standard library.
    This is a drop-in replacement.
    
    Args:
        val: String to convert ('y', 'yes', 't', 'true', 'on', '1' → True)
                              ('n', 'no', 'f', 'false', 'off', '0' → False)
    
    Returns:
        Boolean value
        
    Raises:
        ValueError: If string is not a valid boolean representation
    """
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    else:
        raise ValueError(f"invalid truth value {val!r}")


class ConversationHandler(ConversationHandlerPort):
    """
    Platform-agnostic conversation orchestrator.

    Handles message processing, UI updates, and learning loop
    without knowing if it's Slack, Telegram, or any other platform.

    This is the Application Layer in Hexagonal Architecture.
    """

    def __init__(
        self,
        coordinator: AgentCoordinator,
        agent_factory: "UserAgentFactory",
        file_service: FileService,
        consolidation_queue: Optional[Any] = None,
        global_config: Optional[ConsolidationSettings] = None,
        security_port: Optional[Any] = None,
        audio_service: Optional[AudioTranscriptionPort] = None,
        rich_content_service: Optional[RichContentService] = None,
        notification_service: Optional[UserNotificationService] = None,
        doc_delivery_service: Optional[DocumentDeliveryService] = None,
        indexed_email_repo: Optional[Any] = None,
        user_repo: Optional[Any] = None,
        # ARCHITECTURE FIX: Injected callback replaces direct import of consolidation_handler.
        # Previously: `from src.handlers.consolidation_handler import process_user_batches_on_overflow`
        # That was a horizontal coupling between two handlers. Now wired in composition/.
        overflow_callback: Optional[Callable[..., Coroutine]] = None,
        localization: Optional[LocalizationService] = None,
        file_conversion_service: Optional["FileConversionService"] = None,
        channel_binding_service: Optional[ChannelBindingService] = None,
        channel_history_source: Optional[Any] = None,
    ):
        self.coordinator = coordinator
        self.agent_factory = agent_factory
        self.file_service = file_service
        self._file_conversion_service = file_conversion_service
        self.consolidation_queue = consolidation_queue
        self.global_config = global_config or ConsolidationSettings()
        self.security_port = security_port  # Phase 4: v3 OUTPUT validation
        self.audio_service = audio_service
        self._rich_content_service = rich_content_service
        self._notification_service = notification_service
        self._doc_delivery_service = doc_delivery_service
        self._indexed_email_repo = indexed_email_repo
        self._user_repo = user_repo
        self._overflow_callback = overflow_callback
        self._localization = localization
        self._fallback_service = AgentFallbackService(coordinator)
        self._channel_binding = channel_binding_service
        self._channel_history = channel_history_source

    async def _deliver_rich_content(
        self,
        content,
        response_channel: ResponseChannel,
        thread_id: Optional[str],
    ) -> None:
        """
        Route rich content to the appropriate delivery path:
          - map_image / file → RichContentService (convert + upload or GCS URL)
          - table and others → ResponseChannel.send_rich_content (Block Kit / fallback)

        When RichContentService.process() returns a URL (e.g. HTML → GCS),
        the URL is sent as a plain message so Slack unfurls it as a preview.
        """
        if content.content_type in _MEDIA_CONTENT_TYPES and self._rich_content_service:
            channel_id = getattr(response_channel, "channel_id", None)
            if channel_id:
                url = await self._rich_content_service.process(content, channel_id)
                if url:
                    await response_channel.send_message(url, thread_id=thread_id)
            else:
                logger.warning(
                    "ConversationHandler: response_channel has no channel_id — "
                    "falling back to send_rich_content for type '%s'",
                    content.content_type,
                )
                await response_channel.send_rich_content(content, thread_id=thread_id)
        else:
            await response_channel.send_rich_content(content, thread_id=thread_id)

    async def _deliver_item(
        self,
        item: DeliveryItem,
        response_channel: ResponseChannel,
        thread_id: Optional[str],
    ) -> None:
        """Dispatch a DeliveryItem to its appropriate handler."""
        if item.type == "html_gcs_link":
            if not self._rich_content_service:
                logger.warning("⚠️ [ConversationHandler] html_gcs_link item but no RichContentService configured")
                return
            try:
                url = await self._rich_content_service._store_html(
                    item.data["html"],
                    item.data.get("filename", "content.html"),
                )
                if url:
                    link_text = item.data.get("link_text", "View details")
                    await response_channel.send_message(f"<{url}|{link_text}>", thread_id)
            except Exception as e:
                logger.warning("⚠️ [ConversationHandler] Failed to deliver html_gcs_link: %s", e)
        elif item.type == "rich_content":
            content = RichContent(
                content_type=item.data["content_type"],
                data=item.data["data"],
                fallback_text=item.data.get("fallback", ""),
            )
            await self._deliver_rich_content(content, response_channel, thread_id)
        elif item.type == "message":
            await response_channel.send_message(item.data["text"], thread_id)
        elif item.type == "file_upload":
            if not self._rich_content_service:
                logger.warning(
                    "⚠️ [ConversationHandler] file_upload item but no RichContentService configured"
                )
                return
            channel_id = getattr(response_channel, "channel_id", None)
            if not channel_id:
                logger.warning(
                    "⚠️ [ConversationHandler] file_upload: response_channel has no channel_id — skipping"
                )
                return
            try:
                file_bytes = base64.b64decode(item.data["file_bytes_b64"])
                await self._rich_content_service.upload_file_bytes(
                    file_bytes=file_bytes,
                    filename=item.data["filename"],
                    title=item.data["title"],
                    channel_id=channel_id,
                )
            except Exception as e:
                logger.error(
                    "⚠️ [ConversationHandler] file_upload failed: %s", e, exc_info=True
                )
        elif item.type == "document":
            if not self._doc_delivery_service:
                logger.warning("⚠️ [ConversationHandler] document item but no DocumentDeliveryService configured")
                return
            try:
                content = base64.b64decode(item.data["content_b64"])
                filename = item.data["filename"]
                label = item.data.get("label", filename)
                url = await self._doc_delivery_service.store(
                    content, filename, item.data["content_type"]
                )
                await response_channel.send_document_link(url=url, label=label, thread_id=thread_id)
                if item.data.get("file_upload"):
                    await response_channel.send_file(
                        content=content,
                        filename=filename,
                        title=label,
                        thread_id=thread_id,
                    )
            except Exception as e:
                logger.error("⚠️ [ConversationHandler] document delivery failed: %s", e, exc_info=True)
        else:
            logger.warning("⚠️ [ConversationHandler] Unknown DeliveryItem type: %s — skipping", item.type)

    async def _get_consolidation_config(self, user_id: str) -> ConsolidationSettings:
        """Resolve consolidation settings for a specific user."""
        try:
            user_profile = await self.agent_factory.user_repo.get_user(user_id)
            if user_profile and user_profile.config:
                return ConsolidationSettings(
                    threshold=user_profile.config.consolidation_threshold or self.global_config.threshold,
                    batch_size=user_profile.config.consolidation_batch_size or self.global_config.batch_size,
                    max_queue_messages=self.global_config.max_queue_messages,
                    max_retry_attempts=self.global_config.max_retry_attempts
                )
        except Exception as e:
            logger.warning(f"⚠️ Failed to load user config for {user_id}: {e}")

        return self.global_config

    async def validate_model_output(self, response_text: str, user_id: str) -> str:
        """
        Validate model output before storing in conversation history (Phase 4).

        This prevents indirect prompt injection:
        - Model generates malicious response
        - Response stored in conversation history
        - Malicious text injected into next prompt

        Args:
            response_text: Model response to validate
            user_id: User identifier for logging context

        Returns:
            Validated (and possibly sanitized) response text

        Raises:
            No exceptions - always returns safe text
        """
        if not self.security_port:
            # Phase 4 MVP: security_port is optional, pass through if not configured
            return response_text

        try:
            # Import locally to avoid circular dependency
            from ..domain.prompt_v3.security import TrustZone, RiskLevel

            # Validate model output (UNTRUSTED zone)
            result = await self.security_port.validate(
                text=response_text,
                context=f"model_output_user_{user_id}",
                zone=TrustZone.UNTRUSTED  # Model output is untrusted
            )

            if result.risk_level == RiskLevel.CRITICAL:
                logger.error(
                    f"🚨 [OUTPUT VALIDATION] Model output BLOCKED for user {user_id[:8]}: "
                    f"patterns={result.patterns_detected}"
                )
                return "[SYSTEM: Response contained unsafe content and was blocked]"

            elif result.risk_level == RiskLevel.HIGH:
                logger.warning(
                    f"⚠️ [OUTPUT VALIDATION] Model output SANITIZED for user {user_id[:8]}: "
                    f"patterns={result.patterns_detected}"
                )
                return result.sanitized_text

            else:
                # SAFE, LOW, MEDIUM → pass through
                return result.sanitized_text

        except Exception as e:
            logger.error(f"❌ [OUTPUT VALIDATION] Validation failed for user {user_id[:8]}: {e}", exc_info=True)
            # On error, pass through original text (fail open to avoid breaking user experience)
            return response_text

    def _resolve_session_mode(self, channel_id: Optional[str], binding: Optional[ChannelBinding]) -> SessionMode:
        """Resolve processing mode based on channel binding."""
        if binding:
            return SessionMode(
                history_source="platform",
                route_intent=binding.intent,
                write_session=False,
                write_consolidation=False,
                update_notification_channel=False,
                use_threads=False,
            )
        return SessionMode()  # default: full orchestrator flow

    async def handle_message(
        self,
        context: MessageContext,
        response_channel: ResponseChannel
    ) -> None:
        set_log_context(session_id=context.session_id, user_id=context.user_id)
        logger.info("🎬 START ConversationHandler.handle_message")
        if context.text:
            logger.info(f"📥 Received text ({len(context.text)} chars)")
        start_time = time.time()

        # --- Resolve session mode from channel binding ---
        channel_id = context.metadata.get("channel")
        binding = None
        if self._channel_binding and channel_id:
            binding = await self._channel_binding.get(channel_id)
        mode = self._resolve_session_mode(channel_id, binding)
        if mode.is_bound:
            logger.info(
                "🔗 [BoundChannel] channel=%s → intent=%s",
                channel_id, mode.route_intent,
            )

        # Persist last active channel for background notifications (best-effort).
        # For Slack DMs, store the Slack user ID (U...) instead of the DM channel ID (D...).
        # chat.postMessage accepts user IDs directly, making notifications resilient to
        # stale DM channel IDs (e.g., after bot reinstall or token rotation).
        if mode.update_notification_channel and self._notification_service and hasattr(response_channel, "platform"):
            channel_id_for_notif = response_channel.channel_id
            if (
                response_channel.platform == "slack"
                and context.metadata.get("channel_type") == "im"
                and context.metadata.get("slack_user_id")
            ):
                channel_id_for_notif = context.metadata["slack_user_id"]
            asyncio.create_task(
                self._notification_service.save_channel(
                    user_id=context.user_id,
                    platform=response_channel.platform,
                    channel_id=channel_id_for_notif,
                )
            )

        message_parts: List[MessagePart] = []
        temp_files = []
        file_part_stubs: dict[int, str] = {}  # id(part) → history stub for converted files

        # Fetch platform history BEFORE sending status message.
        # Status message goes to the channel and becomes raw[0] in Slack API response.
        # If fetched after, exclude_last drops status instead of current user message.
        platform_history = []
        if mode.history_source == "platform" and self._channel_history:
            fetch_channel = channel_id or context.metadata.get("channel", "")
            if fetch_channel:
                platform_history = await self._channel_history.fetch(
                    channel_id=fetch_channel, limit=30,
                )

        stop_event = asyncio.Event()
        current_status_phrase = ""
        dots_count = 1
        thread_id_for_reply = context.thread_id if mode.use_threads else None
        status_message_id, current_status_phrase = await response_channel.send_status_with_phrase(
            StatusType.THINKING,
            thread_id=thread_id_for_reply
        )

        async def update_status_animation(message_id: str):
            nonlocal dots_count
            while not stop_event.is_set():
                # ✅ Throttled: 5 seconds instead of 1 (reduces API spam)
                # Telegram rate limit: 1 msg/sec per chat
                # Status updates every 5 sec = more reasonable for production
                await asyncio.sleep(5)
                if stop_event.is_set():
                    break
                dots_count = (dots_count % 5) + 1
                try:
                    await response_channel.update_status_with_phrase_and_dots(
                        message_id,
                        current_status_phrase,
                        dots_count
                    )
                except Exception:
                    break

        ui_task = asyncio.create_task(update_status_animation(status_message_id))

        async def stop_status_updates():
            stop_event.set()
            if ui_task:
                ui_task.cancel()
                try:
                    await ui_task
                except asyncio.CancelledError:
                    logger.debug("Status update task cancelled")

        try:
            # FALLBACK: If no text but has attachments → use localized file prompt
            if not context.text and context.attachments:
                first_attachment = context.attachments[0]
                mime_type = first_attachment.mime_type.lower()
                if self._localization:
                    from ..domain.language import LanguageCode
                    lang = LanguageCode.from_str(context.language, default=LanguageCode.UK)
                    context.text = self._localization.get_file_prompt(lang, mime_type)
                else:
                    from ..locales import uk
                    if mime_type.startswith("image/"):
                        context.text = uk.FILE_FALLBACK_IMAGE
                    elif mime_type.startswith("video/"):
                        context.text = uk.FILE_FALLBACK_VIDEO
                    elif mime_type == "application/pdf":
                        context.text = uk.FILE_FALLBACK_PDF
                    elif mime_type.startswith("application/") or mime_type.startswith("text/"):
                        context.text = uk.FILE_FALLBACK_DOCUMENT
                    else:
                        context.text = uk.FILE_FALLBACK_GENERIC
                logger.info(f"📎 File without text ({mime_type}) - using fallback: '{context.text}'")
            
            if context.text:
                message_parts.append(MessagePart(text=context.text))

            if context.attachments:
                new_phrase = await response_channel.get_status_phrase(StatusType.PROCESSING_FILE)
                current_status_phrase = new_phrase
                dots_count = 1
                await response_channel.update_status_with_phrase_and_dots(status_message_id, new_phrase, dots_count)

                for attachment in context.attachments:
                    if not attachment.url:
                        logger.warning(f"⚠️ Skipping attachment without URL: {attachment.filename}")
                        continue

                    local_path = await response_channel.download_file(
                        attachment.url,
                        attachment.mime_type
                    )
                    if local_path:
                        temp_files.append(local_path)
                        if self._file_conversion_service:
                            # New path: upload to GCS, return reference-only MessagePart
                            file_part = await self._file_conversion_service.process_attachment(
                                local_path,
                                attachment.filename or "unknown",
                                attachment.mime_type,
                                user_id=context.user_id,
                            )
                        elif is_native_binary(attachment.mime_type):
                            # Legacy fallback: images and PDFs as binary
                            file_part = MessagePart(file_data={
                                "path": local_path,
                                "mime_type": attachment.mime_type
                            })
                        else:
                            # Legacy fallback: convert to text inline
                            text = await convert_file_to_text(
                                local_path,
                                attachment.filename or "unknown",
                                attachment.mime_type,
                                audio_service=self.audio_service,
                            )
                            file_part = MessagePart(text=text)
                            file_part_stubs[id(file_part)] = make_history_stub(
                                text, attachment.filename or "unknown"
                            )
                        message_parts.append(file_part)
                        logger.info(f"📎 File prepared: {attachment.filename} ({attachment.mime_type})")
                    else:
                        logger.warning(f"⚠️ Failed to download file: {attachment.filename}")

            # SESSION_27: Set Request Context for implicit multi-tenant operations
            # All search_facts() calls inside will automatically use account_id
            from ..domain.request_context import RequestContext

            async with RequestContext(
                user_id=context.user_id,
                account_id=context.account_id
            ):
                await self.agent_factory.ensure_agents_for_user(context.user_id)
                session_store = self.agent_factory.get_session_store()

                # Bound channels: strip binary path so adapters don't inline file content.
                # Agent accesses files via open_file delegation instead.
                if mode.is_bound:
                    for part in message_parts:
                        if part.file_data and "path" in part.file_data:
                            part.file_data = {k: v for k, v in part.file_data.items() if k != "path"}

                agent_context = {
                    "session_id": context.session_id,
                    "user_id": context.user_id,
                    "account_id": context.account_id,
                    "thread_id": context.thread_id,
                    "metadata": context.metadata,
                    "current_message_parts": message_parts,
                    "origin_channel_id": channel_id,
                    "origin_platform": getattr(response_channel, "platform", "slack"),
                }
                if platform_history:
                    agent_context["history"] = [m.model_dump() for m in platform_history]

                if mode.is_bound:
                    # Direct delegation — bypass Router
                    with start_span("conversation.bound_agent_response"):
                        response = await self.coordinator.handle_delegation(
                            intent=mode.route_intent,
                            query=context.text,
                            context=agent_context,
                            calling_agent_id="bound_channel",
                        )
                else:
                    # Standard flow — Router triage
                    message = AgentMessage.create(
                        sender="conversation_handler",
                        recipient=f"router_agent_{context.user_id}",
                        intent=AgentIntent.QUERY,
                        payload={
                            "text": context.text or "",
                            "attachments": [
                                {
                                    "filename": attachment.filename,
                                    "mime_type": attachment.mime_type,
                                    "size_bytes": attachment.size_bytes
                                }
                                for attachment in context.attachments
                            ]
                        },
                        context=agent_context,
                        timeout_ms=None
                    )

                    with start_span("conversation.agent_response"):
                        response = await self.coordinator.route_message(message)
                        response = await self._fallback_service.try_quick_fallback(
                            response, context, message_parts
                        )

            await stop_status_updates()

            if response.status != AgentStatus.SUCCESS:
                await response_channel.send_status(StatusType.ERROR, thread_id=thread_id_for_reply)
                return

            response_payload = response.result
            structured_data = None

            # Unified Response Handling (RFC: HISTORY_OPTIMIZATION)
            if isinstance(response_payload, SmartResponse):
                response_text = response_payload.text
                structured_data = response_payload.structured_data
                response_link_list = response_payload.link_list or []
            else:
                # Legacy fallback
                response_text = str(response_payload) if response_payload is not None else ""
                structured_data = None
                response_link_list = []

            # History text starts as full response — will be replaced by summary after Slack delivery
            history_text = response_text

            # Edge case: Rich Content Only (no text)
            if structured_data and not response_text.strip():
                # Send rich content without text update
                await response_channel.update_message(
                    status_message_id,
                    "✅ Відповідь готова."
                )
                await self._deliver_rich_content(
                    structured_data, response_channel, thread_id_for_reply
                )

                # History fallback for rich-only responses
                if not history_text.strip():
                    history_text = structured_data.fallback_text
            
            else:
                # Standard text response (with optional rich content)
                if not response_text.strip():
                    response_text = "*(порожня відповідь від моделі)*"

                # Validate User Output
                response_text = await self.validate_model_output(response_text, context.user_id)
                
                if mode.use_threads:
                    # Standard: chunked with thread support
                    await response_channel.send_chunked_message(
                        response_text,
                        status_message_id,
                        thread_id=context.thread_id,
                        link_list=response_link_list or None,
                    )
                else:
                    # Bound channel: top-level flat response (stays in conversations.history)
                    await response_channel.send_flat_response(response_text, status_message_id)

                if structured_data:
                    await self._deliver_rich_content(
                        structured_data, response_channel, thread_id_for_reply
                    )

            # Resolve history_summary after Slack delivery (task was running concurrently)
            # SESSION_2026-02-18: Async postprocessing — summary generates while Slack delivers
            if ENABLE_HISTORY_OPTIMIZATION and response.metadata:
                summary_task = response.metadata.get("response_summary_task")
                if summary_task:
                    try:
                        summary = await asyncio.wait_for(asyncio.shield(summary_task), timeout=10.0)
                        if summary:
                            history_text = summary
                            logger.debug("💾 [History] Async summary ready (%d chars)", len(history_text))
                        else:
                            logger.warning("💾 [History] Summary empty, using full text")
                    except asyncio.TimeoutError:
                        logger.warning("💾 [History] Summary task timed out, using full text")
                    except Exception as e:
                        logger.warning("💾 [History] Summary task failed: %s, using full text", e)
                elif "response_summary" in response.metadata:
                    history_text = response.metadata["response_summary"] or response_text
                    logger.debug("💾 [History] Using pre-computed summary")

            # Validate History Output
            history_text = await self.validate_model_output(history_text, context.user_id)

            # Clean up temporary file paths before saving to history
            # Files with "path" are temporary and will be deleted in finally block
            # Adapter already processed them during request (uploaded to API or encoded)
            clean_message_parts = []
            for part in message_parts:
                if part.file_data and "ref" in part.file_data and "path" in part.file_data:
                    # GCS-backed file: strip temp path, keep reference
                    clean_file_data = {k: v for k, v in part.file_data.items() if k != "path"}
                    clean_message_parts.append(MessagePart(
                        text=part.text,
                        file_data=clean_file_data,
                    ))
                elif part.file_data and "path" in part.file_data:
                    # Legacy: temporary file without GCS ref — skip entirely
                    logger.debug(f"Skipping temporary file from history: {part.file_data.get('path')}")
                    continue
                elif id(part) in file_part_stubs:
                    # Legacy: converted text files — text=stub, full_text=full
                    clean_message_parts.append(MessagePart(
                        text=file_part_stubs[id(part)],
                        full_text=part.text,
                    ))
                else:
                    clean_message_parts.append(part)

            # Append *_context blocks to full_text for tiered history loading.
            # Any metadata key ending in "_context" is persisted — orchestrators control what they expose.
            for ctx_key, ctx_value in (response.metadata or {}).items():
                if ctx_key.endswith("_context") and ctx_value:
                    context_block = json.dumps(
                        {ctx_key: ctx_value},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    response_text = response_text + "\n\n" + context_block
                    logger.info(
                        "💾 [History] %s appended to full_text: %d chars",
                        ctx_key, len(context_block)
                    )

            # Append rich_content to full_text so LLM sees delivered structured data in history.
            # Only in full_text (not history_text/summary) — subject to HISTORY_FULL_TURNS tiering.
            if structured_data:
                rich_block = json.dumps(
                    {"rich_content": {"type": structured_data.content_type, "data": structured_data.data}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                response_text = response_text + "\n\n" + rich_block
                logger.info(
                    "💾 [History] rich_content appended to full_text: type=%s, %d chars",
                    structured_data.content_type, len(rich_block)
                )

            # Attach consolidation_text from save_to_memory delegations to the user message.
            # consolidation_text is invisible to agents/adapters; only the consolidation
            # serializer reads it (p.consolidation_text or p.text for user parts).
            consolidation_texts = (response.metadata or {}).get("consolidation_text", [])
            if consolidation_texts:
                combined = "\n".join(consolidation_texts) if isinstance(consolidation_texts, list) else str(consolidation_texts)
                clean_message_parts.append(MessagePart(consolidation_text="\n\n" + combined))
                logger.info(
                    "💾 [History] consolidation_text attached to user message (%d chars)", len(combined)
                )

            # Dispatch typed delivery items (e.g. grounding attribution widget from WebSearchAgent).
            for item in response.delivery_items:
                await self._deliver_item(item, response_channel, thread_id_for_reply)

            # Save to History — skip for bound channels (platform API is the session store)
            if mode.write_session:
                await self._save_history_with_retry(
                    session_store=session_store,
                    session_id=context.session_id,
                    user_parts=clean_message_parts,
                    history_text=history_text,
                    response_text=response_text,
                    owner_id=context.user_id,
                )

            logger.info(f"🏁 END ConversationHandler.handle_message ({time.time() - start_time:.2f}s)")

        except Exception as e:
            await stop_status_updates()
            logger.error(f"❌ [ConversationHandler] Error: {e}", exc_info=True)
            try:
                await response_channel.send_status(StatusType.ERROR, thread_id=context.thread_id)
                await response_channel.send_message(
                    "Something went wrong on my end. Please try again.",
                    thread_id=context.thread_id,
                )
            except Exception:
                logger.warning("Failed to send error response to user", exc_info=True)

        finally:
            for path in temp_files:
                try:
                    os.remove(path)
                except Exception:
                    logger.debug("Failed to remove temp file %s", path)

    async def _save_history_with_retry(
        self,
        session_store,
        session_id: str,
        user_parts: list,
        history_text: str,
        response_text: str,
        owner_id: str,
        max_attempts: int = 3,
    ) -> None:
        """Append conversation turn to history with retry for transient gRPC errors."""
        _TRANSIENT = ("RST_STREAM", "UNAVAILABLE", "INTERNAL")
        messages = [
            Message(role="user", parts=user_parts),
            Message(role="model", parts=[MessagePart(text=history_text, full_text=response_text)]),
        ]
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await session_store.append_messages_batch(session_id, messages, owner_id=owner_id)
                if attempt > 1:
                    logger.info(f"✅ History saved after {attempt} attempts")
                return
            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts and any(t in str(exc) for t in _TRANSIENT):
                    delay = 0.5 * attempt
                    logger.warning(f"⚠️ History save attempt {attempt} failed ({exc}), retrying in {delay}s…")
                    await asyncio.sleep(delay)
                else:
                    raise

        raise last_exc  # unreachable, but satisfies type checker

    async def handle_command(
        self,
        command: str,
        context: MessageContext,
        response_channel: ResponseChannel
    ) -> None:
        # SESSION 2026-02-08: Wrap all command handling in RequestContext for consistency
        from ..domain.request_context import RequestContext
        
        async with RequestContext(user_id=context.user_id, account_id=context.account_id):
            if command == "admin_cache_reset":
                logger.warning(f"🔥 ADMIN: Cache reset command received from user {context.user_id[:8]}")
                
                # ARCHITECTURE FIX: Use facade instead of reaching into factory internals.
                # Was: self.agent_factory.assembly_service.invalidate_cache() (Law of Demeter violation)
                try:
                    self.agent_factory.invalidate_prompt_cache()
                    await response_channel.send_message(
                        "✅ **Cache reset complete**\n\n"
                        "All prompt assembly caches have been cleared. "
                        "Next requests will rebuild prompts from Firestore.\n\n"
                        "_Note: This is a global operation affecting all users in this worker process._",
                        thread_id=context.thread_id
                    )
                    logger.info(f"✅ ADMIN: Cache reset successful (user {context.user_id[:8]})")
                except Exception as e:
                    logger.error(f"❌ ADMIN: Cache reset failed: {e}")
                    await response_channel.send_message(
                        f"❌ **Cache reset failed:** `{str(e)}`",
                        thread_id=context.thread_id
                    )
                    
            elif command == "consolidate":
                logger.info(f"⚡ Manual consolidation trigger for user {context.user_id}")
                
                if not self.consolidation_queue:
                    await response_channel.send_message("❌ Consolidation queue is not initialized.", thread_id=context.thread_id)
                    return

                # Ensure agents are ready
                await self.agent_factory.ensure_agents_for_user(context.user_id)
                session_store = self.agent_factory.get_session_store()
                
                # Load current session
                session = await session_store.load_session(context.session_id)
                if not session or not session.messages:
                    await response_channel.send_message("ℹ️ No messages in current session to consolidate.", thread_id=context.thread_id)
                    return

                config = await self._get_consolidation_config(context.user_id)
                
                # For manual trigger, we extract messages manually since we want immediate consolidation
                old_messages = session.extract_oldest_messages(count=config.batch_size)
                if not old_messages:
                     await response_channel.send_message("ℹ️ Not enough messages to consolidate.", thread_id=context.thread_id)
                     return

                # Serialize for consolidation.
                # Model: summary (p.text), not full_text (verbose + web_search_context).
                # User: consolidation_text (explicit save) or user text.
                serialized = []
                for msg in old_messages:
                    if msg.role == "model":
                        parts = [{"text": p.text} for p in msg.parts if p.text]
                    else:
                        parts = [{"text": p.consolidation_text or p.text} for p in msg.parts if p.consolidation_text or p.text]
                    serialized.append({
                        "role": msg.role,
                        "parts": parts,
                        "created_at": msg.created_at,
                    })

                from src.domain.consolidation import ConsolidationBatch
                batch = ConsolidationBatch(
                    user_id=session.user_id,
                    session_id=session.session_id,
                    messages=serialized
                )
                
                await response_channel.send_status(StatusType.THINKING, thread_id=context.thread_id)
                
                # Enqueue
                batch_id = await self.consolidation_queue.enqueue_batch(batch)
                
                # Save trimmed session
                await session_store.save_session(session.session_id, session)
                
                # Process — await keeps HTTP request alive → full CPU on Cloud Run
                if self._overflow_callback:
                    await self._overflow_callback(
                        user_id=context.user_id,
                        coordinator=self.coordinator,
                        agent_factory=self.agent_factory,
                        queue=self.consolidation_queue,
                        indexed_email_repo=self._indexed_email_repo,
                        user_repo=self._user_repo,
                    )

                system_alert = (
                    f"Consolidation of conversation history is complete. "
                    f"{len(serialized)} messages were processed and new facts extracted into memory."
                )
                if self._notification_service:
                    await self._notification_service.notify(
                        user_id=context.user_id,
                        account_id=context.account_id,
                        system_alert=system_alert,
                    )
                else:
                    await response_channel.send_message(
                        f"✅ Consolidation complete: {len(serialized)} messages processed.",
                        thread_id=context.thread_id
                    )
            elif command.startswith("agent"):
                await self._handle_agent_command(command, context, response_channel)

            elif command == "primary":
                await self._handle_primary_command(context, response_channel)

            elif command in ("new", "reset"):
                # Topic marker — acknowledged by bot. SlackChannelHistorySource
                # uses these as history boundaries (fetch stops at marker).
                await response_channel.send_message(
                    "New topic. History cleared.", thread_id=context.thread_id,
                )

            else:
                await response_channel.send_message(
                    f"Невідома команда: `{command}`",
                    thread_id=context.thread_id
                )

    # ------------------------------------------------------------------
    # Channel Binding commands
    # ------------------------------------------------------------------

    async def _handle_agent_command(
        self, command: str, context: MessageContext, response_channel: ResponseChannel,
    ) -> None:
        """Handle $agent <type>, $agent off, $agent (status)."""
        if not self._channel_binding:
            await response_channel.send_message(
                "Channel binding is not configured.", thread_id=context.thread_id,
            )
            return

        channel_id = context.metadata.get("channel")
        if not channel_id:
            await response_channel.send_message(
                "Cannot determine channel ID.", thread_id=context.thread_id,
            )
            return

        parts = command.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        registry = self.coordinator._registry

        if not arg:
            # $agent — show current binding status
            binding = await self._channel_binding.get(channel_id)
            if binding:
                await response_channel.send_message(
                    f"This channel is bound to `{binding.agent_type}` (intent: `{binding.intent}`).\n"
                    f"Use `$agent off` to unbind.",
                    thread_id=context.thread_id,
                )
            else:
                await response_channel.send_message(
                    "This channel is not bound.\n"
                    "Use `$agent list` to see available agents.",
                    thread_id=context.thread_id,
                )
            return

        if arg == "list":
            # $agent list — show all bindable agents (internal flag is LLM visibility, not binding)
            if registry is None:
                await response_channel.send_message(
                    "Agent registry not available.", thread_id=context.thread_id,
                )
                return
            available = sorted(
                d.agent_type for d in registry.list_agents()
                if d.capabilities
            )
            if available:
                lines = [f"  `{a}`" for a in available]
                await response_channel.send_message(
                    f"Available agents:\n" + "\n".join(lines),
                    thread_id=context.thread_id,
                )
            else:
                await response_channel.send_message(
                    "No agents available for binding.",
                    thread_id=context.thread_id,
                )
            return

        if arg == "off":
            await self._channel_binding.unbind(channel_id)
            await response_channel.send_message(
                "Channel unbound. Normal routing restored.", thread_id=context.thread_id,
            )
            return

        # $agent <agent_type> — bind channel
        agent_type = arg
        if registry is None:
            await response_channel.send_message(
                "Agent registry not available.", thread_id=context.thread_id,
            )
            return

        # Find descriptor by agent_type (any agent with capabilities is bindable)
        descriptor = None
        for desc in registry.list_agents():
            if desc.agent_type == agent_type and desc.capabilities:
                descriptor = desc
                break

        if descriptor is None:
            available = sorted(
                d.agent_type for d in registry.list_agents()
                if d.capabilities
            )
            await response_channel.send_message(
                f"Unknown or internal agent type: `{agent_type}`.\n"
                f"Available: {', '.join(f'`{a}`' for a in available)}",
                thread_id=context.thread_id,
            )
            return

        # Use the first capability intent as primary
        intent = next(iter(descriptor.capabilities), None)
        if intent is None:
            await response_channel.send_message(
                f"Agent `{agent_type}` has no capabilities.", thread_id=context.thread_id,
            )
            return

        binding = ChannelBinding(
            channel_id=channel_id,
            agent_type=agent_type,
            intent=intent,
            created_by=context.user_id,
        )
        await self._channel_binding.bind(binding)
        await response_channel.send_message(
            f"Channel bound to `{agent_type}` (intent: `{intent}`). "
            f"All messages will go directly to this agent.\n"
            f"Use `$agent off` to unbind.",
            thread_id=context.thread_id,
        )

    async def _handle_primary_command(
        self, context: MessageContext, response_channel: ResponseChannel,
    ) -> None:
        """Handle $primary — set this channel as primary notification destination."""
        if not self._notification_service:
            await response_channel.send_message(
                "Notification service not configured.", thread_id=context.thread_id,
            )
            return

        channel_id = context.metadata.get("channel")
        if not channel_id:
            await response_channel.send_message(
                "Cannot determine channel ID.", thread_id=context.thread_id,
            )
            return

        # Bound channels cannot be primary
        if self._channel_binding:
            binding = await self._channel_binding.get(channel_id)
            if binding:
                await response_channel.send_message(
                    "Bound channels cannot be primary. "
                    "Run `$primary` in an unbound channel.",
                    thread_id=context.thread_id,
                )
                return

        platform = getattr(response_channel, "platform", "slack")
        await self._notification_service.save_primary(
            user_id=context.user_id,
            platform=platform,
            channel_id=channel_id,
        )
        await response_channel.send_message(
            "This channel is now the primary notification destination.\n"
            "Reminders, daily reviews, and system alerts will be delivered here.",
            thread_id=context.thread_id,
        )
