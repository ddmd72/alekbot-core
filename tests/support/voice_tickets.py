"""Voice-ticket records the way the code stores them (SECRETS_AT_REST_RFC §2.5, S4), for seeding and asserting.

Store keys come from `src/domain/voice_ticket_keys.py` under the test digest key `DIGEST_KEY` (the value the voice
blueprints and `VoiceCallSetupService` get in the tests); a hand-out record's `ticket_ct` is sealed and opened with
the record's own AAD, `voice/<record key>`, as `voice_webhook_app` and `voice_web_call_app` do.
"""
from src.domain import voice_ticket_keys
from src.ports.cipher_port import CipherPort

DIGEST_KEY = b"k" * 32


def ticket_key(ticket: str) -> str:
    return voice_ticket_keys.ticket_key(DIGEST_KEY, ticket)


def call_kind_key(ticket: str) -> str:
    return voice_ticket_keys.call_kind_key(DIGEST_KEY, ticket)


def amd_key(ticket: str) -> str:
    return voice_ticket_keys.amd_key(DIGEST_KEY, ticket)


def result_key(ticket: str, call_id: str) -> str:
    return voice_ticket_keys.result_key(DIGEST_KEY, ticket, call_id)


def abandoned_key(ticket: str, call_id: str) -> str:
    return voice_ticket_keys.abandoned_key(DIGEST_KEY, ticket, call_id)


async def seal_ticket(cipher: CipherPort, record_key: str, ticket: str) -> str:
    """The `ticket_ct` of the hand-out record stored at `record_key`."""
    return await cipher.encrypt(ticket, aad=f"voice/{record_key}")


async def open_ticket(cipher: CipherPort, record_key: str, record: dict) -> str:
    """The ticket a hand-out record at `record_key` carries; raises if the ciphertext is not bound to that key."""
    return await cipher.decrypt(record["ticket_ct"], aad=f"voice/{record_key}")
