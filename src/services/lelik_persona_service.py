from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.entities import FactDomain
from ..domain.lelik_context import LelikContext
from ..domain.llm import Message
from ..domain.user import UserBotConfig
from ..ports.repository import FactRepository
from ..ports.session_store import SessionStore
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..domain.notification import NotificationChannel
    from .user_notification_service import UserNotificationService

# The primary channel's last K messages, as stored: a model turn's `text` is already its
# ≤300-char response_summary, so the slice costs no LLM call and no latency at pickup.
HISTORY_MESSAGES = 30
# A user turn is stored in full (pastes, forwarded mail) — bound each entry.
MAX_ENTRY_CHARS = 500

_KNOWN_DOMAINS = frozenset(d.value for d in FactDomain)


class LelikPersonaService:
    """Assembles the context Lelik's prompt is built from (§4.8); `LelikAgent` builds the prompt.

    Lelik starts warm: the whole biographical cache minus the user's
    `voice_excluded_fact_domains`, standing directives, the primary channel's
    recent history, and the local date/time. See decisions/lelik_warm_context.md.
    """

    def __init__(
        self,
        fact_repository: FactRepository,
        session_store: SessionStore,
        notification_service: "UserNotificationService",
        config: UserBotConfig,
    ) -> None:
        self._facts = fact_repository
        self._sessions = session_store
        self._notifications = notification_service
        self._config = config

    async def assemble(self, user_id: str, account_id: str) -> LelikContext:
        """Raises on a fact-store failure — the caller fails the call closed."""
        facts = await self._facts.get_biographical_context_cached(account_id)
        return LelikContext(
            biographical_facts=self._without_excluded(facts or []),
            conversation_history=await self._recent_history(user_id),
        )

    async def primary_channel(self, user_id: str) -> Optional["NotificationChannel"]:
        # The chain notify_call_summary writes through: Lelik reads, and delegates
        # into, the session his own call summaries land in.
        return await self._notifications.resolve_channel(user_id)

    def _without_excluded(self, facts: List[Dict]) -> List[Dict]:
        excluded = set(self._config.voice_excluded_fact_domains)
        unknown = excluded - _KNOWN_DOMAINS
        if unknown:
            logger.warning(f"[LelikPersona] Ignoring unknown voice_excluded_fact_domains: {sorted(unknown)}")
        return [f for f in facts if isinstance(f, dict) and f.get("domain") not in excluded]

    async def _recent_history(self, user_id: str) -> List[Dict]:
        channel = await self.primary_channel(user_id)
        if channel is None:
            logger.info(f"[LelikPersona] No primary/last-active channel for {user_id[:8]}, no history slice")
            return []
        session = await self._sessions.load_session(f"{user_id}:{channel.channel_id}")
        tz = self._timezone()
        entries = []
        for message in (session.history or [])[-HISTORY_MESSAGES:]:
            entry = self._to_entry(message, tz)
            if entry is not None:
                entries.append(entry)
        return entries

    def _to_entry(self, message: Message, tz: ZoneInfo) -> Optional[Dict]:
        if message.role not in ("user", "model"):
            return None
        text = " ".join(p.text.strip() for p in message.parts if p.text and not p.tool_call).strip()
        if not text:
            return None
        if len(text) > MAX_ENTRY_CHARS:
            text = text[:MAX_ENTRY_CHARS].rstrip() + "…"
        entry = {"role": "user" if message.role == "user" else "alek", "content": text}
        if message.created_at:
            entry["timestamp"] = datetime.fromtimestamp(message.created_at, tz=tz).strftime("%b %d, %H:%M")
        return entry

    def _timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._config.timezone or "UTC")
        except (ZoneInfoNotFoundError, KeyError):
            return ZoneInfo("UTC")
