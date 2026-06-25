"""
UserNotificationService — sends background notifications to a user via their last active channel.

Flow (notify):
  1. Load last active channel from NotificationStatePort.
  2. Create a ResponseChannel via NotificationChannelFactoryPort.
  3. Send system_alert to QuickAgent (via AgentCoordinator) to format in user's communication style.
  4. Deliver the formatted response via ResponseChannel.
  5. Save both turns to session history via SessionStore (if configured).

Callers:
  - fire_due_reminders worker (proactive self-reminders)
  - email indexing worker (batch progress / completion)
  - deep_research_polling worker (async research delivery)
  - AgentWorkerHandler (async document delivery via notify_document_link / notify_file_bytes)
"""
from datetime import datetime, timezone
from typing import Mapping, Optional, Protocol, TYPE_CHECKING

from ..domain.agent import AgentMessage, AgentIntent, AgentResponse, AgentStatus
from ..domain.llm import Message, MessagePart
from ..domain.messaging import SmartResponse
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import NotifyResult
from ..domain.request_context import RequestContext
from ..domain.user import PerformanceTier
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..ports.notification_state_port import NotificationStatePort
from ..ports.platform_media_port import PlatformMediaPort
from ..ports.session_store import SessionStore
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..domain.notification import NotificationChannel


class MessageRouter(Protocol):
    """Protocol for routing agent messages. Implemented by AgentCoordinator."""

    async def route_message(self, message: AgentMessage) -> AgentResponse: ...


# Lightweight structural protocol for the SLA value object — avoids
# importing infrastructure/notification_sla here (services/ may not
# depend on infrastructure/ per REQ-ARCH-01). Composition wires the
# concrete table at construction time.
#
# tier_overrides is optional per-tier timeout supersession. notify()
# resolves: ``sla.tier_overrides.get(tier, sla.timeout_ms)`` when the
# caller provides ``tier=``; falls back to ``sla.timeout_ms`` otherwise.
class _SLA(Protocol):
    timeout_ms: int
    tier_overrides: Mapping[PerformanceTier, int]


class UserNotificationService:

    def __init__(
        self,
        state_repo: NotificationStatePort,
        channel_factory: NotificationChannelFactoryPort,
        coordinator: MessageRouter,
        notification_sla: Mapping[NotificationKind, _SLA],
        platform_media: Optional[PlatformMediaPort] = None,
        session_store: Optional[SessionStore] = None,
    ):
        self._state_repo = state_repo
        self._channel_factory = channel_factory
        self._coordinator = coordinator
        self._notification_sla = notification_sla
        self._platform_media: Optional[PlatformMediaPort] = platform_media
        self._session_store: Optional[SessionStore] = session_store

    async def save_channel(self, user_id: str, platform: str, channel_id: str) -> None:
        """
        Persist last active channel for a user.
        Called by ConversationHandler on every incoming message. Best-effort: never raises.
        """
        try:
            await self._state_repo.save(user_id, platform, channel_id)
        except Exception as exc:
            logger.warning(f"[Notification] Failed to save channel for {user_id[:8]}: {exc}")

    async def save_primary(self, user_id: str, platform: str, channel_id: str) -> None:
        """Set the primary notification channel. Called by $primary command."""
        try:
            await self._state_repo.save_primary(user_id, platform, channel_id)
            logger.info(
                f"📌 [Notification] Primary channel set: {platform}/{channel_id} "
                f"for user {user_id[:8]}"
            )
        except Exception as exc:
            logger.warning(f"[Notification] Failed to save primary for {user_id[:8]}: {exc}")

    async def _resolve_channel(
        self, user_id: str, channel_id_override: Optional[str] = None,
        platform_override: Optional[str] = None,
    ) -> Optional["NotificationChannel"]:
        """
        Resolve notification destination. Fallback chain:
        1. Explicit override (origin_channel_id from async task)
        2. Primary channel ($primary command)
        3. Last active channel (legacy fallback)
        """
        from ..domain.notification import NotificationChannel as NC
        if channel_id_override and platform_override:
            return NC(
                user_id=user_id,
                platform=platform_override,
                channel_id=channel_id_override,
                updated_at=datetime.now(timezone.utc),
            )

        # Try primary first
        try:
            primary = await self._state_repo.get_primary(user_id)
            if primary:
                return primary
        except Exception as exc:
            logger.debug("[Notification] get_primary failed for %s: %s", user_id[:8], exc)

        # Fallback to last active
        try:
            return await self._state_repo.get(user_id)
        except Exception as exc:
            logger.warning(f"[Notification] Failed to load channel for {user_id[:8]}: {exc}")
            return None

    async def notify_raw(
        self,
        user_id: str,
        account_id: str,
        text: str,
        channel_id_override: Optional[str] = None,
        platform_override: Optional[str] = None,
    ) -> None:
        """
        Deliver text directly to user's channel. No agent reformatting.

        Uses fallback chain: override → primary → last active.
        """
        channel_info = await self._resolve_channel(
            user_id, channel_id_override, platform_override,
        )
        if not channel_info:
            logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping")
            return

        response_channel = self._channel_factory.create(
            platform=channel_info.platform,
            channel_id=channel_info.channel_id,
        )
        if not response_channel:
            logger.warning(
                f"[Notification] Cannot create channel: platform={channel_info.platform}"
            )
            return

        try:
            # send_long_text so a long raw body threads instead of being truncated;
            # no link_list here (notify_raw delivers verbatim text, no anchor resolution).
            await response_channel.send_long_text(text)
            logger.info(
                f"📬 [Notification] Raw delivery to {channel_info.platform} "
                f"channel={channel_info.channel_id} user={user_id[:8]}"
            )
        except Exception as exc:
            logger.error(
                f"[Notification] Raw delivery failed for {user_id[:8]} "
                f"(platform={channel_info.platform} channel={channel_info.channel_id}): {exc}",
                exc_info=True,
            )

    async def notify(
        self,
        user_id: str,
        account_id: str,
        system_alert: str,
        *,
        kind: NotificationKind,
        tier: Optional[PerformanceTier] = None,
        agent_id_override: Optional[str] = None,
        session_id: Optional[str] = None,
        save_history: bool = True,
        system_alert_summary: Optional[str] = None,
        framing_suffix: str = " Your response to this message will be read by the user. Inform them of the event details in your usual manner of communication.",
        thinking_effort: Optional[str] = None,
        task_complexity: Optional[str] = None,
        email_for_triage: Optional[list] = None,
        channel_id_override: Optional[str] = None,
        platform_override: Optional[str] = None,
        suppress_transient_retry: bool = False,
        storage_class: Optional[str] = None,
    ) -> NotifyResult:
        """
        Send a background notification to the user's channel.

        Returns a :class:`NotifyResult` reporting whether delivery
        actually completed; callers decide what to do with non-delivery
        (retry, mark idempotency state, escalate).

        Args:
            kind: classification of this notification (drives the SLA
                budget — ``timeout_ms`` propagated to ``AgentMessage``).
                Required keyword-only — every caller picks explicitly,
                no implicit default.
            tier: optional ``PerformanceTier`` that the underlying agent
                will execute on. When the SLA for ``kind`` declares
                tier-specific overrides (e.g. REMINDER), passing tier
                selects the matching budget; otherwise ignored.
                Callers that know the tier (e.g. reminder worker reading
                ``note.complexity``) should pass it; others may omit.
            session_id: if provided, reuses the original conversation
                session (e.g. deep research delivering back to the
                user's thread). Defaults to a session keyed on the
                resolved channel id.
            channel_id_override: if provided, delivers to this channel
                instead of primary/last-active. Used for async results
                that go back to the originating channel.
            suppress_transient_retry: set by callers whose /worker handler
                returns 5xx on failure (reminder, daily email review), so
                Cloud Tasks retries the whole task. Suppresses the agent's
                in-process transient retry to avoid layer1 × layer2
                amplification — the outer queue retry is the single retry.
        """
        sla = self._notification_sla[kind]
        # Resolve effective timeout: tier override wins if both the
        # caller passed a tier AND the SLA declares an override for it.
        if tier is not None and tier in sla.tier_overrides:
            effective_timeout_ms = sla.tier_overrides[tier]
        else:
            effective_timeout_ms = sla.timeout_ms

        channel_info = await self._resolve_channel(
            user_id, channel_id_override, platform_override,
        )
        if not channel_info:
            logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping")
            return NotifyResult(
                delivered=False,
                agent_status=AgentStatus.SUCCESS,
                error="no_channel",
            )

        response_channel = self._channel_factory.create(
            platform=channel_info.platform,
            channel_id=channel_info.channel_id,
        )
        if not response_channel:
            logger.warning(
                f"[Notification] Cannot create channel: platform={channel_info.platform}"
            )
            return NotifyResult(
                delivered=False,
                agent_status=AgentStatus.SUCCESS,
                error=f"channel_factory_returned_none:{channel_info.platform}",
            )

        recipient = agent_id_override or f"quick_response_agent_{user_id}"
        effective_session_id = session_id if session_id is not None else f"{user_id}:{channel_info.channel_id}"
        framed = MessagePart(text=f"[System: {system_alert}{framing_suffix}]")
        message = AgentMessage.create(
            sender="notification_service",
            recipient=recipient,
            intent=AgentIntent.QUERY,
            payload={"text": ""},
            context={
                "user_id": user_id,
                "account_id": account_id,
                "session_id": effective_session_id,
                "thread_id": None,
                "current_message_parts": [framed],
                **({"thinking_effort": thinking_effort} if thinking_effort else {}),
                **({"task_complexity": task_complexity} if task_complexity else {}),
                **({"email_for_triage": email_for_triage} if email_for_triage else {}),
                # Propagated to any document the agent generates in this run, so the
                # delivery funnel files it under the right prefix (gating + TTL).
                **({"storage_class": storage_class} if storage_class else {}),
                # Cloud-Task-backed delivery: outer queue retry covers transients,
                # so the agent must not also retry in-process (avoids layer1 × layer2).
                **({"suppress_transient_retry": True} if suppress_transient_retry else {}),
            },
            timeout_ms=effective_timeout_ms,
        )

        try:
            async with RequestContext(user_id=user_id, account_id=account_id):
                response = await self._coordinator.route_message(message)
            if response.status != AgentStatus.SUCCESS:
                logger.warning(
                    f"[Notification] Agent returned {response.status} for user {user_id[:8]}"
                )
                return NotifyResult(
                    delivered=False,
                    agent_status=response.status,
                    error=getattr(response, "error", None),
                )

            result = response.result
            text = ""
            link_list: list = []
            rich_content = None
            if isinstance(result, SmartResponse):
                text = result.text
                link_list = result.link_list or []
                rich_content = result.structured_data
            else:
                text = str(result) if result else ""

            if text:
                # Prepend user mention for Slack so the message triggers a notification sound.
                # channel_id is a Slack user ID (U...) when stored from a DM conversation.
                if channel_info.platform == "slack" and channel_info.channel_id.startswith("U"):
                    text = f"<@{channel_info.channel_id}> {text}"

                # The channel owns the single-vs-thread decision: it measures the
                # RENDERED length (after link resolution + formatting), so a body
                # that fits raw but overflows once [N] anchors expand into full
                # links is threaded instead of truncated.
                await response_channel.send_long_text(text, link_list=link_list or None)
                logger.info(
                    f"📬 [Notification] Sent to {channel_info.platform} "
                    f"channel={channel_info.channel_id} user={user_id[:8]} kind={kind.value}"
                )

                if self._session_store and save_history:
                    try:
                        meta = getattr(response, "metadata", None) or {}
                        history_summary = meta.get("response_summary") or text
                        await self._session_store.append_messages_batch(
                            session_id=effective_session_id,
                            owner_id=user_id,
                            messages=[
                                # When the caller supplies a compact summary, tier the user
                                # turn like a file part: text=short label, full_text=full alert.
                                # Recent turns keep the full alert; aged-out turns shrink to the
                                # label (see BaseAgent._apply_history_tiering). Without a summary,
                                # full_text stays None → no tiering (unchanged behaviour).
                                Message(role="user", parts=[MessagePart(
                                    text=system_alert_summary or system_alert,
                                    full_text=system_alert if system_alert_summary else None,
                                )]),
                                Message(role="model", parts=[MessagePart(text=history_summary, full_text=text)]),
                            ],
                        )
                    except Exception as exc:
                        logger.warning("[Notification] Failed to save history: %s", exc)

            if rich_content:
                await response_channel.send_rich_content(rich_content)

            return NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS)

        except Exception as exc:
            logger.error(
                f"[Notification] Failed to send notification for {user_id[:8]} "
                f"(platform={channel_info.platform} channel={channel_info.channel_id} "
                f"kind={kind.value}): {exc}",
                exc_info=True,
            )
            return NotifyResult(
                delivered=False,
                agent_status=AgentStatus.FAILED,
                error=str(exc),
            )

    async def notify_document_link(
        self,
        user_id: str,
        account_id: str,
        url: str,
        label: str,
        channel_id_override: Optional[str] = None,
        platform_override: Optional[str] = None,
        key: Optional[str] = None,
    ) -> None:
        """
        Send a named document link to the user's channel.
        Uses fallback chain: override → primary → last active.

        key — internal storage object key. When provided, it (not the user-facing
        URL) is written to conversation history so the agent can re-read the
        document later via open_file (server-side, not via an external URL fetch).
        """
        channel_info = await self._resolve_channel(
            user_id, channel_id_override, platform_override,
        )
        if not channel_info:
            logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping document link delivery")
            return

        response_channel = self._channel_factory.create(
            platform=channel_info.platform,
            channel_id=channel_info.channel_id,
        )
        if not response_channel:
            logger.warning(
                f"[Notification] Cannot create channel for document link: platform={channel_info.platform}"
            )
            return

        try:
            await response_channel.send_document_link(url=url, label=label)
            logger.info(
                f"📎 [Notification] Document link delivered to {channel_info.platform} "
                f"channel={channel_info.channel_id} user={user_id[:8]} label={label}"
            )
            if self._session_store:
                if key:
                    # Internal key → agent re-reads server-side via open_file (no
                    # external fetch, not bound to the user link's TTL).
                    history_note = (
                        f"[Document delivered to user: {label}.\n"
                        f'If the user asks about it, read the full content using the '
                        f'open_file intent with context={{"file_ref": "{key}"}}.]'
                    )
                else:
                    history_note = (
                        f"[Document delivered to user: {label} — {url}\n"
                        f"If the user asks about it, you can read the full content using the fetch_url intent.]"
                    )
                doc_session_id = f"{user_id}:{channel_info.channel_id}"
                await self._session_store.append_messages_batch(
                    session_id=doc_session_id,
                    owner_id=user_id,
                    messages=[
                        Message(role="user", parts=[MessagePart(text=f"[System: async document ready — {label}]")]),
                        Message(role="model", parts=[MessagePart(text=history_note, full_text=history_note)]),
                    ],
                )
        except Exception as exc:
            logger.error(
                f"[Notification] Document link delivery failed for {user_id[:8]} "
                f"(platform={channel_info.platform}): {exc}",
                exc_info=True,
            )

    async def notify_file_bytes(
        self,
        user_id: str,
        account_id: str,
        file_bytes: bytes,
        filename: str,
        title: str,
        channel_id_override: Optional[str] = None,
        platform_override: Optional[str] = None,
    ) -> None:
        """
        Upload a file to the user's channel via PlatformMediaPort.
        Uses fallback chain: override → primary → last active.

        Channel ID resolution:
        The stored channel_id may be a Slack user ID (U...) — see ConversationHandler.
        chat.postMessage accepts user IDs and returns the real DM channel ID (D...) in
        the response. We send a placeholder emoji via response_channel.send_message(),
        which normalises channel_id to D... (SlackResponseChannel.send_message updates
        self.channel_id from the API response). The resolved ID is then passed to
        platform_media.upload_file(), which requires a proper channel ID.
        """
        if not self._platform_media:
            logger.warning(
                f"[Notification] notify_file_bytes: platform_media not configured for {user_id[:8]}"
            )
            return

        channel_info = await self._resolve_channel(
            user_id, channel_id_override, platform_override,
        )
        if not channel_info:
            logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping file delivery")
            return

        response_channel = self._channel_factory.create(
            platform=channel_info.platform,
            channel_id=channel_info.channel_id,
        )
        if not response_channel:
            logger.warning(
                f"[Notification] Cannot create channel for file delivery: platform={channel_info.platform}"
            )
            return

        try:
            # Sending a message resolves a Slack user ID (U...) to the real DM channel ID (D...).
            # SlackResponseChannel.send_message() updates self.channel_id from the API response.
            await response_channel.send_message("📎")
            resolved_channel_id = response_channel.channel_id

            await self._platform_media.upload_file(
                file_bytes=file_bytes,
                filename=filename,
                title=title,
                channel_id=resolved_channel_id,
            )
            logger.info(
                f"📎 [Notification] File delivered to {channel_info.platform} "
                f"channel={resolved_channel_id} user={user_id[:8]} filename={filename}"
            )
        except Exception as exc:
            logger.error(
                f"[Notification] File delivery failed for {user_id[:8]} "
                f"(platform={channel_info.platform} channel={channel_info.channel_id}): {exc}",
                exc_info=True,
            )
