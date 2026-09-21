from typing import Callable, Optional

import httpx

from src.domain.voice_call_buffer import VoiceCallBuffer
from src.ports.call_control_plane_port import CallControlPlanePort


class HttpCallControlPlaneAdapter(CallControlPlanePort):
    """Relay-side only. Lives in adapters/, not services/, so REQ-ARCH-18's
    httpx ban (services/ only) does not apply here."""

    def __init__(self, main_service_url: str, id_token_provider: Callable[[], str], http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._base_url = main_service_url.rstrip("/")
        self._id_token_provider = id_token_provider
        self._client = http_client or httpx.AsyncClient()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._id_token_provider()}"}

    async def fetch_session_config(self, ticket: str) -> dict:
        response = await self._client.post(
            f"{self._base_url}/voice/session-config", json={"ticket": ticket}, headers=self._headers()
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
            f"{self._base_url}/voice/submit-transcript", json=payload, headers=self._headers()
        )
        response.raise_for_status()
