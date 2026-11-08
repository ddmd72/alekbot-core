"""
WebCallMedia — the web call's record and media legs, shared by the two entry points that end a running web call:
the Cabinet web-call blueprint (`/hangup`, and a new web start replacing the running call, VOICE_CALL_CONTROL_CHANNEL_RFC
§7.5.2) and the Twilio dial-in (a phone dial replacing the running web call, §7.5.4).
"""
from typing import Optional

from src.domain.voice_ticket_keys import ticket_key


class WebCallMedia:
    """Reads a web call's record (`voice_web_call:{call_id}`) and ends its media. Ending closes the SFU adapters, so the
    relay sees the egress gone and runs the normal end path (end-of-call report, marker release); a call whose ticket
    the relay never redeemed is released here, since nothing else will. Neither the button marker nor the record is
    touched: what a hangup adds to that is the caller's."""

    def __init__(self, media_room, ephemeral_store, call_setup, cipher, digest_key: bytes) -> None:
        self._media_room = media_room
        self._store = ephemeral_store
        self._call_setup = call_setup
        self._cipher = cipher
        self._digest_key = digest_key

    @staticmethod
    def record_key(call_id: str) -> str:
        return f"voice_web_call:{call_id}"

    async def record(self, call_id: str, user_id: str) -> Optional[dict]:
        """The call's record when it exists and belongs to `user_id`, else None."""
        record = await self._store.get(self.record_key(call_id))
        if record is None or record.get("user_id") != user_id:
            return None
        return record

    async def ticket(self, call_id: str, record: dict) -> str:
        return await self._cipher.decrypt(record["ticket_ct"], aad=f"voice/{self.record_key(call_id)}")

    async def close(self, record: dict, ticket: str, user_id: str) -> None:
        await self._media_room.close(record.get("adapter_ids", []))
        # Ticket still unredeemed = the relay never started: nothing will release the marker.
        if await self._store.get(ticket_key(self._digest_key, ticket)) is not None:
            await self._call_setup.release(ticket, user_id)
