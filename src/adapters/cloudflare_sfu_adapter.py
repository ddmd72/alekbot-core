from typing import List, Optional

import httpx

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg
from src.ports.media_room_port import MediaRoomError, MediaRoomPort
from src.utils.logger import logger

_API = "https://rtc.live.cloudflare.com/v1/apps/{app_id}"
_TIMEOUT_S = 15.0
# Track names are ours; the relay and the page never see them.
_CALLER_TRACK = "mic"
_AGENT_TRACK = "lelik"


class CloudflareSfuAdapter(MediaRoomPort):
    """Cloudflare Realtime SFU over its HTTPS API. Request order and shapes: POC
    scripts/voice/cloudflare_sfu_poc/poc.py (authoritative). The App Secret stays server-side."""

    def __init__(self, app_id: str, app_secret: str, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._base = _API.format(app_id=app_id)
        self._headers = {"Authorization": f"Bearer {app_secret}"}
        self._client = http_client or httpx.AsyncClient(timeout=_TIMEOUT_S)

    async def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        response = await self._client.request(method, f"{self._base}{path}", headers=self._headers, json=body)
        if response.status_code >= 400:
            logger.error(f"[CloudflareSfu] {method} {path} -> {response.status_code}: {response.text[:300]}")
            raise MediaRoomError(f"{method} {path} returned {response.status_code}")
        return response.json() if response.status_code != 204 else {}

    async def open_caller(self, offer_sdp: str, mid: str) -> CallerLeg:
        session_id = (await self._call("POST", "/sessions/new"))["sessionId"]
        answer = await self._call("POST", f"/sessions/{session_id}/tracks/new", {
            "sessionDescription": {"type": "offer", "sdp": offer_sdp},
            "tracks": [{"location": "local", "mid": mid, "trackName": _CALLER_TRACK}],
        })
        return CallerLeg(session_id=session_id, answer_sdp=answer["sessionDescription"]["sdp"])

    async def attach_agent(self, caller_session_id: str, ingest_url: str, egress_url: str) -> AgentLeg:
        adapter_ids: List[str] = []
        try:
            ingest = (await self._call("POST", "/adapters/websocket/new", {"tracks": [{
                "location": "local", "trackName": _AGENT_TRACK, "inputCodec": "pcm", "endpoint": ingest_url,
            }]}))["tracks"][0]
            adapter_ids.append(ingest["adapterId"])
            egress = (await self._call("POST", "/adapters/websocket/new", {"tracks": [{
                "location": "remote", "sessionId": caller_session_id, "trackName": _CALLER_TRACK,
                "outputCodec": "pcm", "endpoint": egress_url,
            }]}))["tracks"][0]
            adapter_ids.append(egress["adapterId"])
            pull = await self._call("POST", f"/sessions/{caller_session_id}/tracks/new", {"tracks": [{
                "location": "remote", "sessionId": ingest["sessionId"], "trackName": _AGENT_TRACK,
            }]})
        except MediaRoomError:
            if adapter_ids:
                await self.close(adapter_ids)
            raise
        offer = (pull.get("sessionDescription") or {}).get("sdp") if pull.get("requiresImmediateRenegotiation") else None
        return AgentLeg(adapter_ids=adapter_ids, offer_sdp=offer)

    async def complete_negotiation(self, caller_session_id: str, answer_sdp: str) -> None:
        await self._call("PUT", f"/sessions/{caller_session_id}/renegotiate",
                         {"sessionDescription": {"type": "answer", "sdp": answer_sdp}})

    async def close(self, adapter_ids: List[str]) -> None:
        for adapter_id in adapter_ids:
            try:
                await self._call("POST", "/adapters/websocket/close", {"tracks": [{"adapterId": adapter_id}]})
            except Exception:
                logger.error(f"[CloudflareSfu] closing adapter {adapter_id} failed", exc_info=True)
