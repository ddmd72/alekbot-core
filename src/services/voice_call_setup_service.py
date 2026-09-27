"""
VoiceCallSetupService — everything a call needs before any audio flows,
whatever the transport (VOICE_WEB_TRANSPORT_RFC §5.5): the one-call-per-user
marker, Lelik's warm session stored on the ticket the relay redeems, and the
call kind.

Extracted from the inline logic in `src/web/voice_webhook_app.py`
(`voice_auth` for the marker/ticket write, `voice_answer` for the persona
assembly + failure handling) so the web entry point (a later task) and the
Twilio webhook can share one implementation instead of two copies drifting
apart.
"""
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from src.ports.alert_sink import AlertSinkPort
from src.ports.ephemeral_store import EphemeralStore
from src.utils.logger import logger

if TYPE_CHECKING:  # type-only: services/ must not import agents/ at runtime
    from src.agents.lelik_agent import LelikAgent


class VoiceCallSetupError(Exception):
    """Lelik's session could not be prepared; ticket and marker are already released."""


class VoiceCallSetupService:
    """Everything a call needs before any audio flows, whatever the transport
    (VOICE_WEB_TRANSPORT_RFC §5.5): the one-call-per-user marker, Lelik's warm session on the
    ticket the relay redeems, and the call kind the pickup note and the summary header follow."""

    def __init__(self, ephemeral_store: EphemeralStore, alert_sink: AlertSinkPort,
                 lelik_agent_provider: Callable[[str], Awaitable[Optional["LelikAgent"]]],
                 ticket_ttl_s: int = 300, one_call_ttl_s: int = 3600) -> None:
        self._store = ephemeral_store
        self._alerts = alert_sink
        self._lelik = lelik_agent_provider
        self._ticket_ttl_s = ticket_ttl_s
        self._one_call_ttl_s = one_call_ttl_s

    @staticmethod
    def _marker_key(user_id: str) -> str:
        return f"voice_one_call:{user_id}"

    async def claim(self, user_id: str, holder: dict, ttl_s: int) -> bool:
        if await self._store.get(self._marker_key(user_id)) is not None:
            logger.warning(f"voice setup: refused - {user_id} already has a call in flight")
            return False
        await self._store.set(self._marker_key(user_id), {"in_flight": True, **holder}, ttl_s=ttl_s)
        return True

    async def holder(self, user_id: str) -> Optional[dict]:
        return await self._store.get(self._marker_key(user_id))

    async def prepare(self, ticket: str, user_id: str, account_id: str, call_kind: str) -> None:
        try:
            agent = await self._lelik(user_id)
            if agent is None:
                raise RuntimeError("voice companion is not configured on this deployment")
            session = await agent.session_config(user_id=user_id, account_id=account_id)
        except Exception as exc:
            logger.error(f"voice setup: persona assembly failed for {user_id}: {exc}", exc_info=True)
            await self.release(ticket, user_id)
            await self._alerts.post(f"Voice: persona assembly failed for user {user_id}: {exc}")
            raise VoiceCallSetupError(str(exc)) from exc
        # identity wins over anything the session carried: /voice/delegate trusts these ids.
        await self._store.set(f"voice_ticket:{ticket}", {
            **session, "user_id": user_id, "account_id": account_id, "call_kind": call_kind,
        }, ttl_s=self._ticket_ttl_s)
        await self._store.set(f"voice_call_kind:{ticket}", {"call_kind": call_kind}, ttl_s=self._one_call_ttl_s)

    async def release(self, ticket: str, user_id: str) -> None:
        await self._store.delete(f"voice_ticket:{ticket}")
        await self._store.delete(self._marker_key(user_id))
