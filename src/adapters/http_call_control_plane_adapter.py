import asyncio
from typing import Callable, Optional

import httpx

from src.domain.voice_call_buffer import VoiceCallBuffer
from src.ports.call_control_plane_port import CallControlPlanePort

# Router -> Smart -> specialists takes tens of seconds; the relay's own wait_for is the
# authoritative limit, this only has to outlast it (httpx defaults to 5 s).
_DELEGATE_TIMEOUT_S = 150.0
# The main side summarizes and flushes before responding.
_SUBMIT_TRANSCRIPT_TIMEOUT_S = 60.0


class HttpCallControlPlaneAdapter(CallControlPlanePort):
    """Relay-side only. Lives in adapters/, not services/, so REQ-ARCH-18's
    httpx ban (services/ only) does not apply here."""

    def __init__(self, main_service_url: str, id_token_provider: Callable[[], str], http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._base_url = main_service_url.rstrip("/")
        self._id_token_provider = id_token_provider
        self._client = http_client or httpx.AsyncClient()

    async def _headers(self) -> dict:
        # fetch_id_token blocks; off the loop so a mid-call request never stalls audio.
        token = await asyncio.to_thread(self._id_token_provider)
        return {"Authorization": f"Bearer {token}"}

    async def fetch_session_config(self, ticket: str) -> dict:
        response = await self._client.post(
            f"{self._base_url}/voice/session-config", json={"ticket": ticket}, headers=await self._headers()
        )
        response.raise_for_status()
        return response.json()

    async def submit_transcript(self, call_id: str, user_id: str, account_id: str, buffer: VoiceCallBuffer) -> None:
        payload = {
            "call_id": call_id,
            "user_id": user_id,
            "account_id": account_id,
            "transcript_text": buffer.transcript_text,
            "usage_by_model": buffer.usage_by_model,
            "turns": [
                {
                    "request_text": t.request_text,
                    "response_text": t.response_text,
                    "started_at": t.started_at.isoformat(),
                    "ended_at": t.ended_at.isoformat(),
                    "finish_reason": t.finish_reason,
                }
                for t in buffer.turns
            ],
        }
        response = await self._client.post(
            f"{self._base_url}/voice/submit-transcript", json=payload, headers=await self._headers(),
            timeout=_SUBMIT_TRANSCRIPT_TIMEOUT_S,
        )
        response.raise_for_status()

    async def delegate(self, user_id: str, account_id: str, arguments: dict, call_context: list) -> str:
        response = await self._client.post(
            f"{self._base_url}/voice/delegate",
            json={"user_id": user_id, "account_id": account_id,
                  "arguments": arguments, "call_context": call_context},
            headers=await self._headers(),
            timeout=_DELEGATE_TIMEOUT_S,
        )
        response.raise_for_status()
        return response.json()["output"]
