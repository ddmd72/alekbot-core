"""Helpers for tests that read or build Twilio callback references (`src/domain/voice_callback_ref.py`)."""
from src.domain.voice_callback_ref import CALLBACK_REF_AAD, from_callback_ref, to_callback_ref


async def callback_ref_for(cipher, ticket: str) -> str:
    """The `ref` query value a callback URL carries for `ticket`."""
    return to_callback_ref(await cipher.encrypt(ticket, aad=CALLBACK_REF_AAD))


async def ticket_of(cipher, ref: str) -> str:
    """The raw ticket behind a callback reference."""
    return await cipher.decrypt(from_callback_ref(ref), aad=CALLBACK_REF_AAD)
