from typing import Optional

from src.domain.voice_resume import RESUME_TTL_S, resume_key
from src.ports.ephemeral_store import EphemeralStore
from src.utils.logger import logger


class VoiceResumeService:
    """The one record that lets a re-dial continue a dropped voice call (VOICE_CALL_CONTROL_CHANNEL_RFC §6.2):
    the provider's conversation handle plus the mode and provider it belongs to, keyed by user, expiring with the
    store's TTL. No transcript content is ever kept here."""

    def __init__(self, ephemeral_store: EphemeralStore, ttl_s: int = RESUME_TTL_S) -> None:
        self._store = ephemeral_store
        self._ttl_s = ttl_s

    async def keep(self, user_id: str, handle: str, mode: str, provider: str) -> None:
        await self._store.set(resume_key(user_id), {"handle": handle, "mode": mode, "provider": provider},
                              ttl_s=self._ttl_s)

    async def take(self, user_id: str, mode: str, provider: str) -> Optional[str]:
        """The handle for a new call of this mode and provider, consumed (one resume per handle); None when there is
        none, it expired, or it belongs to another mode or provider (that record is left to its TTL)."""
        record = await self._store.get(resume_key(user_id))
        if record is None:
            return None
        if record.get("mode") != mode or record.get("provider") != provider:
            logger.info(f"voice resume: record kept for another mode or provider, call opens fresh (mode={mode})")
            return None
        claimed = await self._store.get_and_delete(resume_key(user_id))
        handle = (claimed or {}).get("handle")
        return handle if isinstance(handle, str) and handle else None
