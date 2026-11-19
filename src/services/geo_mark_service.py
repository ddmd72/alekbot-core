"""GeoMarkService — the whole geo mark flow every entry point calls (GEO_MARKS_RFC §4.4, §4.5).

`locate` geocodes a fix (never raises). `record` renders the mark, posts the confirmation line to the channel and
appends one user/model pair to the session (user: the mark text; model: the posted line), so roles keep alternating.
The Slack `$geo` link is a bot-issued capability: `issue_link` stores a `GeoLinkBinding` under the keyed digest of a
128-bit opaque id for ten minutes and returns the URL; `peek_link` reads it without consuming it (the page's GET);
`redeem_link` takes it once (the page's POST) and either records the mark or queues the owner's question as a
`geo_mark_turn` worker task. Every step runs before the caller answers: Cloud Run throttles CPU once it has.

Logs never carry coordinates, addresses or the raw link id: the source, the accuracy band, `address=yes|no`, the
user id prefix and the link fingerprint only.
"""
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, Optional

from src.domain.geo_mark import (
    LINK_DIGEST_PURPOSE, LINK_ID_PATTERN, LINK_TTL_S, GeoFix, GeoLinkBinding, GeoMark, accuracy_band,
    local_shared_at, render_mark_text,
)
from src.domain.language import LanguageCode
from src.domain.llm import Message, MessagePart
from src.domain.messaging import ResponseChannel
from src.domain.secret_digest import fingerprint, keyed_digest
from src.domain.ui_messages import UIMessage
from src.ports.ephemeral_store import EphemeralStore
from src.ports.localization_port import LocalizationPort
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.reverse_geocoding_port import ReverseGeocodingPort
from src.ports.session_store import SessionStore
from src.ports.task_queue import TaskQueue
from src.ports.user_repository import UserRepository
from src.utils.logger import logger

TURN_DEADLINE_S = 1800
LINK_PATH = "/geo/"
_KEY_PREFIX = "geo_link:"

PAGE_MESSAGES = (
    UIMessage.GEO_PAGE_LOCATING, UIMessage.GEO_PAGE_SAVED, UIMessage.GEO_PAGE_SAVED_NO_ADDRESS,
    UIMessage.GEO_PAGE_QUESTION_SENT, UIMessage.GEO_PAGE_QUESTION_NOT_SENT, UIMessage.GEO_PAGE_DENIED,
    UIMessage.GEO_PAGE_DENIED_HOWTO, UIMessage.GEO_PAGE_UNAVAILABLE, UIMessage.GEO_PAGE_RETRY,
    UIMessage.GEO_PAGE_GONE, UIMessage.GEO_PAGE_NOT_SAVED,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GeoRedeemStatus(str, Enum):
    SAVED = "saved"                         # no text: the pair is in history
    TURN_QUEUED = "turn_queued"             # text: the question runs as a queued turn with the mark
    QUESTION_NOT_SENT = "question_not_sent"  # text, the enqueue failed: the pair is in history instead
    GONE = "gone"                           # unknown, expired, already used or malformed link
    NOT_SAVED = "not_saved"                 # the link was taken, but nothing reached history


@dataclass(frozen=True)
class GeoRedeem:
    status: GeoRedeemStatus
    mark: Optional[GeoMark] = None


class GeoMarkService:

    def __init__(self, *, links: EphemeralStore, geocoder: Optional[ReverseGeocodingPort], sessions: SessionStore,
                 channels: NotificationChannelFactoryPort, task_queue: TaskQueue, users: UserRepository,
                 localization: LocalizationPort, digest_key: bytes, base_url: str,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._links = links
        self._geocoder = geocoder
        self._sessions = sessions
        self._channels = channels
        self._task_queue = task_queue
        self._users = users
        self._localization = localization
        self._digest_key = digest_key
        self._base_url = base_url.rstrip("/")
        self._clock = clock

    # ------------------------------------------------------------------ marks

    async def locate(self, fix: GeoFix, language: str) -> GeoMark:
        """The fix and its nearest street address; no geocoder, or a lookup that fails, gives a mark without one."""
        address: Optional[str] = None
        if self._geocoder is not None:
            try:
                address = await self._geocoder.nearest_address(fix.latitude, fix.longitude, language=language)
            except Exception as exc:     # the port never raises; a broken adapter still never fails a mark
                logger.warning("[GeoMark] geocoder raised: %s", type(exc).__name__)
                address = None
        return GeoMark(fix=fix, address=address)

    async def record(self, mark: GeoMark, *, user_id: str, session_id: str, channel: ResponseChannel,
                     language: str) -> str:
        """Post the confirmation line to `channel` and append the user/model pair to `session_id`; returns the line.
        A failed post is logged and the pair is still appended; a failed append is logged and raised."""
        zone = await self._zone(user_id)
        confirmation = self._confirmation(mark, zone, language)
        await self._post(channel, confirmation, user_id)
        await self._append_pair(render_mark_text(mark, zone), confirmation, user_id=user_id, session_id=session_id)
        self._log("recorded", mark, user_id)
        return confirmation

    # ------------------------------------------------------------------ links

    async def issue_link(self, binding: GeoLinkBinding) -> str:
        """Store the binding for LINK_TTL_S under the raw id's keyed digest and return `<base_url>/geo/<raw id>`."""
        raw_id = secrets.token_urlsafe(16)
        digest = self._digest(raw_id)
        await self._links.set(_KEY_PREFIX + digest, binding.to_record(), ttl_s=LINK_TTL_S)
        logger.info("[GeoMark] link issued: user=%s link=%s text=%s", binding.user_id[:8], fingerprint(digest),
                    "yes" if binding.text else "no")
        return f"{self._base_url}{LINK_PATH}{raw_id}"

    async def peek_link(self, raw_id: str) -> Optional[GeoLinkBinding]:
        """The live binding of a link, without consuming it; None for a wrong-shaped id (no store read), an unknown,
        expired or malformed link."""
        if not LINK_ID_PATTERN.fullmatch(raw_id):
            return None
        digest = self._digest(raw_id)
        return self._binding(await self._links.get(_KEY_PREFIX + digest), digest)

    async def redeem_link(self, raw_id: str, fix: GeoFix) -> GeoRedeem:
        """RFC §4.5: take the link once, locate, post the confirmation to the bound channel, then append the pair (no
        text) or queue the question as a `geo_mark_turn` task (text; a failed enqueue appends the pair instead)."""
        if not LINK_ID_PATTERN.fullmatch(raw_id):
            return GeoRedeem(GeoRedeemStatus.GONE)
        digest = self._digest(raw_id)
        binding = self._binding(await self._links.get_and_delete(_KEY_PREFIX + digest), digest)
        if binding is None:
            logger.info("[GeoMark] link gone: link=%s", fingerprint(digest))
            return GeoRedeem(GeoRedeemStatus.GONE)
        mark = await self.locate(fix, binding.language)
        zone = await self._zone(binding.user_id)
        confirmation = self._confirmation(mark, zone, binding.language)
        mark_text = render_mark_text(mark, zone)
        channel = self._channels.create(binding.platform, binding.channel_id)
        if channel is None:
            logger.error("[GeoMark] no %s channel to post the confirmation: user=%s", binding.platform,
                         binding.user_id[:8])
        else:
            await self._post(channel, confirmation, binding.user_id)
        if binding.text:
            try:
                await self._task_queue.enqueue_worker_task("geo_mark_turn", {
                    "platform": binding.platform, "user_id": binding.user_id, "account_id": binding.account_id,
                    "channel_id": binding.channel_id, "session_id": binding.session_id,
                    "platform_user_id": binding.platform_user_id, "text": binding.text, "mark_text": mark_text,
                    "language": binding.language, "link_fp": fingerprint(digest),
                }, deadline_seconds=TURN_DEADLINE_S, dedup_key=digest)
            except Exception as exc:
                logger.error("[GeoMark] turn enqueue failed: user=%s link=%s: %s", binding.user_id[:8],
                             fingerprint(digest), type(exc).__name__)
                return await self._save_pair(mark, mark_text, confirmation, binding,
                                             GeoRedeemStatus.QUESTION_NOT_SENT)
            self._log("turn queued", mark, binding.user_id)
            return GeoRedeem(GeoRedeemStatus.TURN_QUEUED, mark)
        return await self._save_pair(mark, mark_text, confirmation, binding, GeoRedeemStatus.SAVED)

    def page_strings(self, language: str) -> Dict[str, str]:
        """The Slack page's strings in `language`, keyed by their `UIMessage` value (templates, unformatted)."""
        lang = LanguageCode.from_str(language, default=LanguageCode.EN)
        return {message.value: self._localization.get_ui_string(lang, message) for message in PAGE_MESSAGES}

    # ------------------------------------------------------------------ internals

    def _digest(self, raw_id: str) -> str:
        return keyed_digest(self._digest_key, LINK_DIGEST_PURPOSE, raw_id)

    @staticmethod
    def _binding(record: Optional[Dict], digest: str) -> Optional[GeoLinkBinding]:
        if record is None:
            return None
        try:
            return GeoLinkBinding.from_record(record)
        except ValueError as exc:
            logger.error("[GeoMark] malformed link record: link=%s: %s", fingerprint(digest), exc)
            return None

    async def _zone(self, user_id: str) -> str:
        try:
            user = await self._users.get_user(user_id)
            return user.config.timezone if user is not None else "UTC"
        except Exception:
            logger.warning("[GeoMark] timezone unreadable for user=%s; using UTC", user_id[:8], exc_info=True)
            return "UTC"

    def _confirmation(self, mark: GeoMark, zone: str, language: str) -> str:
        lang = LanguageCode.from_str(language, default=LanguageCode.EN)
        accuracy = "±?" if mark.fix.accuracy_m is None else f"±{round(mark.fix.accuracy_m)} m"
        time = local_shared_at(mark, zone).strftime("%H:%M")
        if mark.address:
            template = self._localization.get_ui_string(lang, UIMessage.GEO_SAVED)
            return template.format(address=mark.address, accuracy=accuracy, time=time)
        template = self._localization.get_ui_string(lang, UIMessage.GEO_SAVED_NO_ADDRESS)
        return template.format(accuracy=accuracy, time=time)

    @staticmethod
    async def _post(channel: ResponseChannel, text: str, user_id: str) -> None:
        try:
            await channel.send_message(text)
        except Exception as exc:
            logger.error("[GeoMark] confirmation post failed: user=%s: %s", user_id[:8], type(exc).__name__)

    async def _append_pair(self, mark_text: str, confirmation: str, *, user_id: str, session_id: str) -> None:
        now = self._clock().timestamp()
        messages = [
            Message(role="user", parts=[MessagePart(text=mark_text)], created_at=now),
            Message(role="model", parts=[MessagePart(text=confirmation, full_text=confirmation)], created_at=now),
        ]
        try:
            await self._sessions.append_messages_batch(session_id, messages, owner_id=user_id, raise_on_error=True)
        except Exception as exc:
            logger.error("[GeoMark] history append failed: user=%s: %s", user_id[:8], type(exc).__name__)
            raise

    async def _save_pair(self, mark: GeoMark, mark_text: str, confirmation: str, binding: GeoLinkBinding,
                         saved: GeoRedeemStatus) -> GeoRedeem:
        try:
            await self._append_pair(mark_text, confirmation, user_id=binding.user_id, session_id=binding.session_id)
        except Exception:
            return GeoRedeem(GeoRedeemStatus.NOT_SAVED, mark)
        self._log("recorded", mark, binding.user_id)
        return GeoRedeem(saved, mark)

    @staticmethod
    def _log(what: str, mark: GeoMark, user_id: str) -> None:
        logger.info("[GeoMark] %s: source=%s accuracy=%s address=%s user=%s", what, mark.fix.source.value,
                    accuracy_band(mark), "yes" if mark.address else "no", user_id[:8])
