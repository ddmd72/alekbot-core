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
# A close of an adapter the SFU no longer has (already closed, or gone after the call ended) is
# the expected outcome of an idempotent hangup, not a failure.
_ALREADY_CLOSED = (404, 410)


class _SfuStatusError(MediaRoomError):
    """The SFU answered with an HTTP error status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class CloudflareSfuAdapter(MediaRoomPort):
    """Cloudflare Realtime SFU over its HTTPS API. Request order and shapes: POC
    scripts/voice/cloudflare_sfu_poc/poc.py (authoritative). The App Secret stays server-side."""

    def __init__(self, app_id: str, app_secret: str, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._base = _API.format(app_id=app_id)
        self._headers = {"Authorization": f"Bearer {app_secret}"}
        self._client = http_client or httpx.AsyncClient(timeout=_TIMEOUT_S)

    async def _call(self, method: str, path: str, body: Optional[dict] = None,
                    log_errors: bool = True) -> dict:
        """Every failure leaves here as MediaRoomError, so callers need one except clause.
        Log lines carry the method, path and exception type only; never the headers (secret).
        `log_errors=False` skips every log line here — the caller owns logging instead. `close()`
        uses this: its failures are best-effort cleanup and must log exactly once, at its own
        level, not at this call's default ERROR plus its own line."""
        try:
            response = await self._client.request(method, f"{self._base}{path}", headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            if log_errors:
                logger.error(f"[CloudflareSfu] {method} {path} failed: {type(exc).__name__}: {exc}")
            raise MediaRoomError(f"{method} {path} failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            if log_errors:
                logger.error(f"[CloudflareSfu] {method} {path} -> {response.status_code}: {response.text[:300]}")
            raise _SfuStatusError(f"{method} {path} returned {response.status_code}", response.status_code)
        # POC behaviour: an empty 2xx body is an empty object.
        if response.status_code == 204 or not response.text:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            if log_errors:
                logger.error(f"[CloudflareSfu] {method} {path} -> {response.status_code}: body is not JSON")
            raise MediaRoomError(f"{method} {path} returned a non-JSON body") from exc

    @staticmethod
    def _adapter_track(response: dict, direction: str) -> dict:
        """The one track of an /adapters/websocket/new response. Cloudflare reports per-track
        failures inside a 2xx body, so a missing adapter is a MediaRoomError like any other."""
        try:
            track = response["tracks"][0]
        except (KeyError, IndexError, TypeError) as exc:
            logger.error(f"[CloudflareSfu] {direction} adapter response has no track")
            raise MediaRoomError(f"{direction} adapter response has no track") from exc
        if track.get("errorCode") or not track.get("adapterId"):
            logger.error(f"[CloudflareSfu] {direction} adapter not created: {track.get('errorCode')}")
            raise MediaRoomError(f"{direction} adapter not created: {track.get('errorCode')}")
        return track

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
            ingest = self._adapter_track(await self._call("POST", "/adapters/websocket/new", {"tracks": [{
                "location": "local", "trackName": _AGENT_TRACK, "inputCodec": "pcm", "endpoint": ingest_url,
            }]}), "ingest")
            adapter_ids.append(ingest["adapterId"])
            egress = self._adapter_track(await self._call("POST", "/adapters/websocket/new", {"tracks": [{
                "location": "remote", "sessionId": caller_session_id, "trackName": _CALLER_TRACK,
                "outputCodec": "pcm", "endpoint": egress_url,
            }]}), "egress")
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
        # Best-effort cleanup: a close failure never blocks hangup, and it logs exactly once (at
        # WARNING, not ERROR) — `log_errors=False` stops `_call` from also logging the same
        # failure at ERROR before it gets here.
        for adapter_id in adapter_ids:
            try:
                await self._call("POST", "/adapters/websocket/close", {"tracks": [{"adapterId": adapter_id}]},
                                 log_errors=False)
            except _SfuStatusError as exc:
                if exc.status_code in _ALREADY_CLOSED:
                    logger.info(f"[CloudflareSfu] adapter {adapter_id} was already closed")
                else:
                    logger.warning(f"[CloudflareSfu] closing adapter {adapter_id} failed: {exc}")
            except Exception as exc:
                logger.warning(f"[CloudflareSfu] closing adapter {adapter_id} failed: {type(exc).__name__}: {exc}")
