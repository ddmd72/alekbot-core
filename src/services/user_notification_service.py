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
  - deep_research_webhook_event / deep_research_failed workers (async research delivery)
  - AgentWorkerHandler (async document delivery via notify_document_link / notify_file_bytes)
"""
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Protocol, TYPE_CHECKING

from ..domain.agent import AgentMessage, AgentIntent, AgentResponse, AgentStatus, DeliveryItem
from ..domain.drive_note import DRIVE_NOTE_ERASE_DELIVERY
from ..domain.drive_note_receipt import (
    drive_note_call_event, drive_note_call_posted, drive_note_late_event, render_drive_note_erase, render_drive_note_receipt,
)
from ..domain.language import LanguageCode
from ..domain.llm import Message, MessagePart
from ..domain.messaging import MEDIA_CONTENT_TYPES, RichContent, SmartResponse, session_id_for
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import NotifyResult
from ..domain.request_context import RequestContext
from ..domain.ui_messages import UIMessage
from ..domain.user import PerformanceTier
from ..domain.voice_call_note import CALL_NOTE_PREFIX
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..ports.language_service_port import LanguageServicePort
from ..ports.notification_state_port import NotificationStatePort
from ..ports.platform_media_port import PlatformMediaPort
from ..ports.session_store import SessionStore
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..domain.notification import NotificationChannel
    from .localization_service import LocalizationService
    from .rich_content_service import RichContentService


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


_SLACK = "slack"


class UserNotificationService:

    def __init__(
        self,
        state_repo: NotificationStatePort,
        channel_factory: NotificationChannelFactoryPort,
        coordinator: MessageRouter,
        notification_sla: Mapping[NotificationKind, _SLA],
        platform_media: Optional[PlatformMediaPort] = None,
        session_store: Optional[SessionStore] = None,
        localization: Optional["LocalizationService"] = None,
        language: Optional[LanguageServicePort] = None,
    ):
        self._state_repo = state_repo
        self._channel_factory = channel_factory
        self._coordinator = coordinator
        self._notification_sla = notification_sla
        # One media port per platform (USER_DRIVE_INDEXING_RFC §26.2 review): a file goes through the port of the
        # channel's own platform. The constructor's port is Slack's (the only one wired before); others are added with
        # register_platform_media.
        self._platform_media: Dict[str, PlatformMediaPort] = {}
        if platform_media is not None:
            self._platform_media[_SLACK] = platform_media
        # One RichContentService per platform: a `file` / `widget` item Smart returns on a background path is delivered
        # as the real file/image through it, exactly as in a chat turn. Registered with register_platform_rich_content.
        self._platform_rich_content: Dict[str, "RichContentService"] = {}
        self._session_store: Optional[SessionStore] = session_store
        # The drive-note receipt posted from a call (DRIVE_FILE_NOTES_RFC §4.9) is rendered here, in the owner's UI
        # language; without localization it is not posted (logged).
        self._localization = localization
        self._language = language

    def attach_localization(self, localization: "LocalizationService", language: LanguageServicePort) -> None:
        """Give the drive-note receipt its UI strings and the owner's language (DRIVE_FILE_NOTES_RFC §4.9). Called once
        by the application graph: the service is built before the localization and the language service, which itself
        takes this service (the `register_platform_media` precedent)."""
        self._localization = localization
        self._language = language

    def register_platform_media(self, platform: str, port: PlatformMediaPort) -> None:
        """The media port `notify_file_bytes` uses for channels of `platform` ("slack", "telegram")."""
        self._platform_media[platform] = port

    def register_platform_rich_content(self, platform: str, service: "RichContentService") -> None:
        """The rich-content delivery `notify` / `notify_answer_copy` use for channels of `platform` ("slack", "telegram")."""
        self._platform_rich_content[platform] = service

    async def _send_rich_content(self, response_channel, platform: str, content: RichContent) -> None:
        """Deliver one rich-content item the way a chat turn does. Without a registered service every type goes to the
        channel's own renderer, which shows only a table: a media type then degrades to its fallback text, so that
        gap is logged."""
        service = self._platform_rich_content.get(platform)
        if service is None:
            if content.content_type in MEDIA_CONTENT_TYPES:
                logger.warning(
                    f"[Notification] No rich-content service for platform={platform}: "
                    f"'{content.content_type}' goes out as fallback text, not as a file"
                )
            await response_channel.send_rich_content(content)
            return
        # A Slack channel id stored as a user id (U...) becomes the DM channel (D...) when a message is sent; an
        # upload needs the real one. The answer text normally went first; when none did, open the DM with a marker.
        if (platform == _SLACK and content.content_type in MEDIA_CONTENT_TYPES
                and str(getattr(response_channel, "channel_id", "")).startswith("U")):
            await response_channel.send_message("📎")
        await service.deliver(content, response_channel)

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

    async def resolve_channel(
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
        literal: bool = False,
    ) -> bool:
        """
        Deliver text directly to user's channel. No agent reformatting.

        `literal=True` is for text the bot did not write (a stored fact quoted in a digest): it goes through
        `ResponseChannel.send_literal`, so nothing in it is formatted, linked or obeyed by the platform (`<!channel>`
        is shown). The default sends the text as the bot's own markdown.

        Uses fallback chain: override → primary → last active. Returns whether the text was sent; delivery
        errors are logged and swallowed (USER_DRIVE_INDEXING_RFC §26.2: the drive notice posts its text only
        after its list was sent). Callers that do not need it ignore the result.
        """
        channel_info = await self.resolve_channel(
            user_id, channel_id_override, platform_override,
        )
        if not channel_info:
            logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping")
            return False

        response_channel = self._channel_factory.create(
            platform=channel_info.platform,
            channel_id=channel_info.channel_id,
        )
        if not response_channel:
            logger.warning(
                f"[Notification] Cannot create channel: platform={channel_info.platform}"
            )
            return False

        try:
            if literal:
                await response_channel.send_literal(text)
            else:
                # send_long_text so a long raw body threads instead of being truncated;
                # no link_list here (notify_raw delivers verbatim text, no anchor resolution).
                await response_channel.send_long_text(text)
            logger.info(
                f"📬 [Notification] Raw delivery to {channel_info.platform} "
                f"channel={channel_info.channel_id} user={user_id[:8]}"
            )
            return True
        except Exception as exc:
            logger.error(
                f"[Notification] Raw delivery failed for {user_id[:8]} "
                f"(platform={channel_info.platform} channel={channel_info.channel_id}): {exc}",
                exc_info=True,
            )
            return False

    async def notify_call_summary(
        self,
        user_id: str,
        account_id: str,
        summary: str,
        call_event: str,
    ) -> None:
        """
        Deliver an end-of-call note (voice companion / Lelik, RFC
        docs/10_rfcs/VOICE_COMPANION_RFC.md §4.9) to the user's channel verbatim
        AND persist it to session history as an unmistakable event pair:
        `call_event` (the `[System: phone call with Lelik, …]` line) as the user
        turn, the delivered note as the model turn. A bare "[System: phone call
        ended]" followed by a third-person report read to Alek as a reply of his
        own that he never gave.

        Always resolves to the primary/last-active channel - there is no
        channel_id_override/session_id to thread through. Lelik has no
        CompanionRecord store (RFC §4.9), so this history append is the only
        durable trace a call ever leaves.
        """
        note = f"{CALL_NOTE_PREFIX}{summary}"
        channel_info = await self.resolve_channel(user_id, None, None)
        if not channel_info:
            logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping call summary delivery")
            return

        response_channel = self._channel_factory.create(
            platform=channel_info.platform,
            channel_id=channel_info.channel_id,
        )
        if not response_channel:
            logger.warning(
                f"[Notification] Cannot create channel for call summary: platform={channel_info.platform}"
            )
            return

        try:
            await response_channel.send_long_text(note)
            logger.info(
                f"📬 [Notification] Call summary delivered to {channel_info.platform} "
                f"channel={channel_info.channel_id} user={user_id[:8]}"
            )
            if self._session_store:
                session_id = session_id_for(user_id, channel_info.channel_id)
                await self._session_store.append_messages_batch(
                    session_id=session_id,
                    owner_id=user_id,
                    messages=[
                        Message(role="user", parts=[MessagePart(text=call_event)]),
                        Message(role="model", parts=[MessagePart(text=note, full_text=note)]),
                    ],
                )
        except Exception as exc:
            logger.error(
                f"[Notification] Call summary delivery failed for {user_id[:8]} "
                f"(platform={channel_info.platform}): {exc}",
                exc_info=True,
            )

    async def notify_drive_note_receipt(self, user_id: str, account_id: str, item: DeliveryItem, *,
                                        call: bool = True, channel_id_override: Optional[str] = None,
                                        platform_override: Optional[str] = None) -> None:
        """Post a drive-note receipt or erase request that Alek produced during a call with Lelik (DRIVE_FILE_NOTES_RFC
        §4.9, D18) to the owner's chat, rendered by the same renderer ConversationHandler uses, and append a history
        pair — `[System: drive note saved during a call with Lelik: <path> ref=… v<n>]`, then a neutral
        `[drive note receipt posted to the chat: <path> v<n>]` (never the note's words: the consolidator reads a model
        turn's text and an erase cannot scrub history) — so "undo that" later in the chat resolves the ref. An erase request posts its line and then the command alone;
        the command never enters history. Resolves the primary / last-active channel, like notify_call_summary.
        `call=False` is a LATE receipt on a chat turn (the write landed after the answer, design Δ10): chat wording in
        the history pair, and the run's origin channel (`channel_id_override` / `platform_override`) when it has one.
        The receipt and the erase request's line are sent literally (the note's words and a path are not the bot's);
        the erase command alone is the bot's own text, sent as a message. Every failure is logged and swallowed: the note is stored whatever happens here."""
        if self._localization is None:
            logger.error(f"[Notification] No localization wired: drive note {item.type} not posted for {user_id[:8]}")
            return
        lang = await self._ui_language(user_id)
        localization = self._localization

        def strings(message: UIMessage) -> str:
            return localization.get_ui_string(lang, message)

        try:
            if item.type == DRIVE_NOTE_ERASE_DELIVERY:
                line, command = render_drive_note_erase(item.data, strings)
                posts = [line, command]
            else:
                posts = [render_drive_note_receipt(item.data, strings)]
            kept = drive_note_call_posted(item.type, item.data)
        except Exception as exc:
            logger.error(f"[Notification] drive note {item.type} could not be rendered for {user_id[:8]}: {exc}",
                         exc_info=True)
            return
        channel_info = await self.resolve_channel(user_id, channel_id_override, platform_override)
        if not channel_info:
            # RFC §4.9: the owner then never sees what was stored, so an operator must hear of it
            logger.error(f"[Notification] No channel for {user_id[:8]}: drive note {item.type} not posted")
            return
        response_channel = self._channel_factory.create(platform=channel_info.platform,
                                                        channel_id=channel_info.channel_id)
        if not response_channel:
            logger.warning(f"[Notification] Cannot create channel for a drive note: platform={channel_info.platform}")
            return
        try:
            await response_channel.send_literal(posts[0])
            if item.type == DRIVE_NOTE_ERASE_DELIVERY:
                await response_channel.send_message(posts[1])
        except Exception as exc:
            logger.error(f"[Notification] drive note {item.type} delivery failed for {user_id[:8]} "
                         f"(platform={channel_info.platform}): {exc}", exc_info=True)
            return
        logger.info(f"📬 [Notification] drive note {item.type} delivered to {channel_info.platform} "
                    f"channel={channel_info.channel_id} user={user_id[:8]}")
        if not self._session_store:
            return
        try:
            await self._session_store.append_messages_batch(
                session_id=f"{user_id}:{channel_info.channel_id}",
                owner_id=user_id,
                messages=[
                    Message(role="user", parts=[MessagePart(text=(drive_note_call_event if call else drive_note_late_event)(
                        item.type, item.data))]),
                    Message(role="model", parts=[MessagePart(text=kept, full_text=kept)]),
                ],
            )
        except Exception as exc:
            logger.error(f"[Notification] drive note history pair failed for {user_id[:8]}: {exc}", exc_info=True)

    async def _ui_language(self, user_id: str) -> LanguageCode:
        if self._language is None:
            return LanguageCode.EN
        try:
            return await self._language.resolve_ui_language(user_id)
        except Exception as exc:
            logger.warning(f"[Notification] UI language lookup failed for {user_id[:8]}: {exc}")
            return LanguageCode.EN

    async def notify_answer_copy(self, user_id: str, account_id: str, answer: SmartResponse) -> None:
        """A copy of an answer Alek gave during a phone call (VOICE_COMPANION §4.10 rule 2):
        links and tables are reading-shaped and always reach chat. Delivery only; the call
        reaches history through its end-of-call summary."""
        channel_info = await self.resolve_channel(user_id)
        if not channel_info:
            logger.info(f"[Notification] No channel for {user_id[:8]}, answer copy skipped")
            return
        response_channel = self._channel_factory.create(platform=channel_info.platform, channel_id=channel_info.channel_id)
        if not response_channel:
            logger.warning(f"[Notification] Cannot create channel for answer copy: platform={channel_info.platform}")
            return
        try:
            await self._send_answer_text(response_channel, channel_info, f"{CALL_NOTE_PREFIX}{answer.text}", answer.link_list or [])
            if answer.structured_data:
                await self._send_rich_content(response_channel, channel_info.platform, answer.structured_data)
        except Exception as exc:
            logger.error(f"[Notification] Answer copy failed for {user_id[:8]}: {exc}", exc_info=True)

    @staticmethod
    async def _send_answer_text(response_channel, channel_info, text: str, link_list: list) -> None:
        # Mention a Slack DM user so the message makes a sound.
        if channel_info.platform == "slack" and channel_info.channel_id.startswith("U"):
            text = f"<@{channel_info.channel_id}> {text}"
        await response_channel.send_long_text(text, link_list=link_list or None)

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
        origin_detail: Optional[str] = None,
        fire_key: Optional[str] = None,
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
            origin_detail / fire_key: who started this run, stamped on the workspace writes it makes
                (AGENT_WORKSPACE_RFC §9): context `run_origin` = "background:<origin_detail>" (falls back to
                the kind), `run_fire_key` = fire_key for a reminder fire.
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

        channel_info = await self.resolve_channel(
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
        effective_session_id = session_id if session_id is not None else session_id_for(user_id, channel_info.channel_id)
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
                # Who started this run, for the workspace's origin labels (AGENT_WORKSPACE_RFC §9). The context
                # is built fresh per call, so an origin never leaks from the caller.
                "run_origin": f"background:{origin_detail or kind.value}",
                **({"run_fire_key": fire_key} if fire_key else {}),
            },
            timeout_ms=effective_timeout_ms,
        )

        sent = False
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
                # The channel owns the single-vs-thread decision: it measures the
                # RENDERED length (after link resolution + formatting), so a body
                # that fits raw but overflows once [N] anchors expand into full
                # links is threaded instead of truncated.
                sent = True
                await self._send_answer_text(response_channel, channel_info, text, link_list)
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
                sent = True
                await self._send_rich_content(response_channel, channel_info.platform, rich_content)

            return NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=sent)

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
                sent=sent,
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
        channel_info = await self.resolve_channel(
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
                if key and key.startswith("video_generation/"):
                    # Video can't be "read" as text — open_file resends it as a
                    # native file attachment instead (FileManagementAgent._fetch_video).
                    history_note = (
                        f"[Video delivered to user: {label}.\n"
                        f'If the user asks to resend/forward this video, call the '
                        f'open_file intent with context={{"file_ref": "{key}"}} — it '
                        f'will be delivered to them as a file attachment, not text.]'
                    )
                elif key:
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
                doc_session_id = session_id_for(user_id, channel_info.channel_id)
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
    ) -> bool:
        """
        Upload a file to the user's channel via PlatformMediaPort.
        Uses fallback chain: override → primary → last active. Returns whether the file was uploaded; errors
        are logged and swallowed (callers that do not need the result ignore it).

        The port is the one registered for the channel's platform; a platform with none returns False before
        anything is posted, so no placeholder is left in a chat whose file never follows.

        Channel ID resolution (Slack only):
        The stored channel_id may be a Slack user ID (U...) — see ConversationHandler.
        chat.postMessage accepts user IDs and returns the real DM channel ID (D...) in
        the response. We send a placeholder emoji via response_channel.send_message(),
        which normalises channel_id to D... (SlackResponseChannel.send_message updates
        self.channel_id from the API response). The resolved ID is then passed to
        platform_media.upload_file(), which requires a proper channel ID. Telegram's
        stored chat id is used as it is.
        """
        if not self._platform_media:
            logger.warning(
                f"[Notification] notify_file_bytes: platform_media not configured for {user_id[:8]}"
            )
            return False

        channel_info = await self.resolve_channel(
            user_id, channel_id_override, platform_override,
        )
        if not channel_info:
            logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping file delivery")
            return False

        media = self._platform_media.get(channel_info.platform)
        if media is None:
            logger.warning(
                f"[Notification] notify_file_bytes: no media port for platform={channel_info.platform} "
                f"(user {user_id[:8]}); nothing posted"
            )
            return False

        response_channel = None
        if channel_info.platform == _SLACK:
            response_channel = self._channel_factory.create(
                platform=channel_info.platform,
                channel_id=channel_info.channel_id,
            )
            if not response_channel:
                logger.warning(
                    f"[Notification] Cannot create channel for file delivery: platform={channel_info.platform}"
                )
                return False

        try:
            resolved_channel_id = channel_info.channel_id
            if response_channel is not None:
                # Sending a message resolves a Slack user ID (U...) to the real DM channel ID (D...).
                # SlackResponseChannel.send_message() updates self.channel_id from the API response.
                await response_channel.send_message("📎")
                resolved_channel_id = response_channel.channel_id

            await media.upload_file(
                file_bytes=file_bytes,
                filename=filename,
                title=title,
                channel_id=resolved_channel_id,
            )
            logger.info(
                f"📎 [Notification] File delivered to {channel_info.platform} "
                f"channel={resolved_channel_id} user={user_id[:8]} filename={filename}"
            )
            return True
        except Exception as exc:
            logger.error(
                f"[Notification] File delivery failed for {user_id[:8]} "
                f"(platform={channel_info.platform} channel={channel_info.channel_id}): {exc}",
                exc_info=True,
            )
            return False
