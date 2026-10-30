"""Store keys derived from a voice ticket (SECRETS_AT_REST_RFC §2.5, S4).

A ticket is a bearer credential: nothing stores it raw. Each key is a keyed digest of the ticket under its own
purpose, so the ticket is recoverable from no key and one purpose's key opens no other purpose's record.
"""
from src.domain.secret_digest import keyed_digest

_SEP = "\x00"


def ticket_key(key: bytes, ticket: str) -> str:
    """The pending-call record the relay redeems."""
    return f"voice_ticket:{keyed_digest(key, 'voice_ticket', ticket)}"


def call_kind_key(key: bytes, ticket: str) -> str:
    """The call kind (`phone` / `web`) the summary header follows."""
    return f"voice_call_kind:{keyed_digest(key, 'voice_call_kind', ticket)}"


def amd_key(key: bytes, ticket: str) -> str:
    """The async answering-machine verdict of a call."""
    return f"voice_amd:{keyed_digest(key, 'voice_amd', ticket)}"


def result_key(key: bytes, ticket: str, call_id: str) -> str:
    """A delegation answer kept for the chat."""
    return f"voice_delegation_result:{keyed_digest(key, 'voice_delegation_result', ticket + _SEP + call_id)}"


def abandoned_key(key: bytes, ticket: str, call_id: str) -> str:
    """The relay's marker that it stopped waiting for a delegation."""
    return f"voice_delegation_abandoned:{keyed_digest(key, 'voice_delegation_abandoned', ticket + _SEP + call_id)}"
