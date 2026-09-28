"""
MediaRoomPort — the SFU a web caller's audio goes through (VOICE_WEB_TRANSPORT_RFC §5.3 #5).

Port justification: a system boundary with a real alternative (Cloudflare Realtime SFU today;
LiveKit or a self-hosted SFU were evaluated).
"""
from abc import ABC, abstractmethod
from typing import List

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg


class MediaRoomError(Exception):
    """The SFU refused or failed a request."""


class MediaRoomPort(ABC):
    @abstractmethod
    async def open_caller(self, offer_sdp: str, mid: str) -> CallerLeg:
        """Publish the caller's microphone (transceiver `mid`) and answer their offer."""

    @abstractmethod
    async def attach_agent(self, caller_session_id: str, ingest_url: str, egress_url: str) -> AgentLeg:
        """Connect the relay: the SFU dials `ingest_url` (Lelik's audio in) and `egress_url` (the
        caller's microphone out), and the caller's session subscribes to Lelik's track."""

    @abstractmethod
    async def complete_negotiation(self, caller_session_id: str, answer_sdp: str) -> None:
        """The caller's answer to AgentLeg.offer_sdp."""

    @abstractmethod
    async def close(self, adapter_ids: List[str]) -> None:
        """Best effort; never raises."""
