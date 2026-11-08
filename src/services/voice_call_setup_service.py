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
import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from src.domain.secret_digest import fingerprint
from src.domain.voice_mode import UnsupportedVoiceMode, VoiceMode
from src.domain.voice_ticket_keys import call_kind_key, one_call_owner, ticket_key
from src.ports.alert_sink import AlertSinkPort
from src.ports.ephemeral_store import EphemeralStore
from src.utils.logger import logger

if TYPE_CHECKING:  # type-only: services/ must not import agents/ at runtime
    from src.agents.lelik_agent import LelikAgent


# The one-call marker's field naming the call that holds it (`one_call_owner` of its ticket). A marker without it (written
# before the field existed) is released by any call of the user, as before.
ONE_CALL_OWNER_FIELD = "owner"


def one_call_marker_key(user_id: str) -> str:
    return f"voice_one_call:{user_id}"


async def release_one_call_marker(store: EphemeralStore, digest_key: bytes, user_id: str, ticket: str) -> bool:
    """Delete the user's one-call marker unless it names another call as its owner; True when deleted.

    A phone dial takes the marker over from a web call that has not released it yet (VOICE_CALL_CONTROL_CHANNEL_RFC
    §7.5.4), so that call's late end-of-call report must leave the new owner's marker alone. The store has no
    compare-and-delete: a takeover landing between this read and the delete is still lost; the phone path rewrites its
    marker when the dial ends (`/voice/inbound-status`), which heals that window. A marker that cannot be read is
    deleted, as before: a stuck marker locks the user out, a lost one only loosens the one-call limit."""
    key = one_call_marker_key(user_id)
    try:
        marker = await store.get(key)
    except Exception:
        logger.error(f"voice call {fingerprint(ticket)}: reading the one-call marker of {user_id} failed, released "
                     f"unconditionally", exc_info=True)
        marker = None
    owner = marker.get(ONE_CALL_OWNER_FIELD) if isinstance(marker, dict) else None
    if isinstance(owner, str) and owner != one_call_owner(digest_key, ticket):
        logger.info(f"voice call {fingerprint(ticket)}: the one-call marker of {user_id} belongs to another call, "
                    f"left in place")
        return False
    await store.delete(key)
    return True


class VoiceCallSetupError(Exception):
    """Lelik's session could not be prepared; ticket and marker are already released."""


class VoiceCallSetupService:
    """Everything a call needs before any audio flows, whatever the transport
    (VOICE_WEB_TRANSPORT_RFC §5.5): the one-call-per-user marker, Lelik's warm session on the
    ticket the relay redeems, and the call kind the pickup note and the summary header follow."""

    def __init__(self, ephemeral_store: EphemeralStore, alert_sink: AlertSinkPort,
                 lelik_agent_provider: Callable[[str], Awaitable[Optional["LelikAgent"]]],
                 *, digest_key: bytes, ticket_ttl_s: int = 300, one_call_ttl_s: int = 3600) -> None:
        self._store = ephemeral_store
        self._digest_key = digest_key
        self._alerts = alert_sink
        self._lelik = lelik_agent_provider
        self._ticket_ttl_s = ticket_ttl_s
        self._one_call_ttl_s = one_call_ttl_s

    @staticmethod
    def _marker_key(user_id: str) -> str:
        return one_call_marker_key(user_id)

    async def claim(self, user_id: str, holder: dict, ttl_s: int) -> bool:
        if await self._store.get(self._marker_key(user_id)) is not None:
            logger.warning(f"voice setup: refused - {user_id} already has a call in flight")
            return False
        await self._store.set(self._marker_key(user_id), {"in_flight": True, **holder}, ttl_s=ttl_s)
        return True

    async def holder(self, user_id: str) -> Optional[dict]:
        return await self._store.get(self._marker_key(user_id))

    def owner_of(self, ticket: str) -> dict:
        """The marker fields that name the call of `ticket` as the holder (merged into a `claim`'s holder)."""
        return {ONE_CALL_OWNER_FIELD: one_call_owner(self._digest_key, ticket)}

    async def wait_released(self, user_id: str, wait_s: float, poll_s: float) -> bool:
        """Poll until the user's one-call marker is gone; False when it is still held after `wait_s`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while await self.holder(user_id) is not None:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(poll_s)
        return True

    async def prepare(self, ticket: str, user_id: str, account_id: str, call_kind: str,
                      mode: VoiceMode = VoiceMode.LELIK, *, control_token_sha256: Optional[str] = None) -> None:
        """`control_token_sha256` (web calls, VOICE_CALL_CONTROL_CHANNEL_RFC §4.1) rides on the ticket record, so the
        relay gets it with the session config; only the digest, never the token."""
        control = {"control_token_sha256": control_token_sha256} if control_token_sha256 else {}
        # The ticket writes sit inside the same failure handling as the persona assembly: on the
        # phone path the marker is already at its full call TTL here, so a store failure that
        # escaped without a release would lock the owner out of calling for an hour.
        try:
            agent = await self._lelik(user_id)
            if agent is None:
                raise RuntimeError("voice companion is not configured on this deployment")
            session = await agent.session_config(user_id=user_id, account_id=account_id, mode=mode)
            # identity wins over anything the session carried: /voice/delegate trusts these ids.
            await self._store.set(ticket_key(self._digest_key, ticket), {
                **session, "user_id": user_id, "account_id": account_id, "call_kind": call_kind, "mode": mode.value,
                **control,
            }, ttl_s=self._ticket_ttl_s)
            await self._store.set(call_kind_key(self._digest_key, ticket), {"call_kind": call_kind, "mode": mode.value}, ttl_s=self._one_call_ttl_s)
        except UnsupportedVoiceMode:
            # A refusal, not a fault: free the marker and ticket, no alert; the caller answers 409.
            logger.warning(f"voice setup: mode {mode.value!r} unavailable for {user_id}")
            await self.release(ticket, user_id)
            raise
        except Exception as exc:
            logger.error(f"voice setup: call preparation failed for {user_id} (ticket {fingerprint(ticket)}): {exc}",
                         exc_info=True)
            try:
                await self.release(ticket, user_id)
            except Exception:
                # Same store that just failed: the marker then expires on its own TTL.
                logger.error(f"voice setup: release after a failed preparation also failed for {user_id}",
                             exc_info=True)
            await self._alerts.post(f"Voice: call preparation failed for user {user_id}: {exc}")
            raise VoiceCallSetupError(str(exc)) from exc

    async def release(self, ticket: str, user_id: str) -> None:
        """Delete the ticket, and the one-call marker while it is still this call's (`release_one_call_marker`)."""
        await self._store.delete(ticket_key(self._digest_key, ticket))
        await release_one_call_marker(self._store, self._digest_key, user_id, ticket)
