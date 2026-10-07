from typing import Any, Dict, List
from abc import ABC, abstractmethod

from src.domain.voice_call_buffer import VoiceCallBuffer


class CallControlPlanePort(ABC):
    """The relay's only edge to the main service (RFC §4.5). Slice 1 shipped
    two operations; Slice 2 adds delegate."""

    @abstractmethod
    async def fetch_session_config(self, ticket: str) -> Dict[str, Any]:
        """Exchange an opaque ticket for the session config the answer
        webhook stashed (persona instructions, resolved identity)."""

    @abstractmethod
    async def submit_transcript(self, call_id: str, user_id: str, account_id: str, buffer: VoiceCallBuffer) -> None:
        """Flush the call-scoped buffer at call end. No retry on failure
        (RFC §4.7 corner-case table: transport failure fails loudly, does
        not re-run).

        user_id/account_id identify which user the call belongs to, needed
        by later Slice 1 work (end-of-call summary delivery, one-call-marker
        release) that consumes the submitted payload on the main-service side.
        """

    @abstractmethod
    async def delegate(self, user_id: str, account_id: str, arguments: Dict[str, Any], call_context: List[Any],
                       ticket: str = "", call_id: str = "", request: str = "") -> str:
        """Run one delegate_to_specialist call from the live session on the main service
        (RFC §4.7) and return the result as text. No retry.

        `ticket` + `call_id` name the delegation so the main side can keep its result and post
        it to chat if the relay stops waiting (`abandon_delegation`); `request` is the short
        label that post is headed with. All three optional: without them nothing is kept."""

    @abstractmethod
    async def abandon_delegation(self, ticket: str, call_id: str) -> None:
        """Tell the main service the relay no longer waits for this delegation (timed out, or
        the call ended): its answer goes to the user's chat instead. Best effort; never raises."""
