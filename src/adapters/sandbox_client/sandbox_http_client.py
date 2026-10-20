"""
SandboxHttpClient: the one HTTP caller of the sandbox service (RFC §4.7 call contract, §4.9 failure semantics,
§4.10, §4.14; plan G10, G11).

`call(op, body, limits)` POSTs the opaque body to `/v1/<op>` with `Authorization: Bearer <ID token>` and the closed
`X-Sandbox-Meta` header (fresh `request_id` per attempt, no identity), and returns the validated outcome and the
response bytes. It never parses the body (§4.7): `ok` is streamed to bytes, a failure's text is the capped body.

Classification is by HTTP status FIRST, then the outcome header (Cloud Run's own IAM 401/403 carry no header):
    401 / 403                       SandboxMisconfigured + ERROR log, whatever the header says
    429, 503 other than recycling,  SandboxUnavailable (systemic, nothing charged)
    connect error / refused
    503 `recycling`                 retried at once, up to 3 attempts, short jitter; still recycling -> Unavailable
    reset / 5xx WITHOUT a valid     one retry on a fresh instance, then outcome `error`, detail `sandbox_lost`
    outcome header, or a timeout
    413                             outcome `too_large`
    anything else                   decided by the valid outcome header (a 500 `error` or 504 `timeout` is an engine
                                    verdict, not a transport loss); no valid header -> outcome `error`
Timeout = the child's wall + 15 s. One log line per call: request_id, op, status, wall; nothing else.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

from ...domain.sandbox_contract import (
    ERROR_TEXT_CAP_BYTES,
    META_HEADER,
    OUTCOME_HEADER,
    RESPONSE_CAP_BYTES,
    SandboxContractError,
    SandboxLimits,
    SandboxMeta,
    SandboxOp,
    SandboxOutcome,
    SandboxStatus,
    decode_outcome,
    encode_meta,
    new_request_id,
)
from ...domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from ...utils.logger import logger
from .metadata_id_token_provider import normalise_audience

TIMEOUT_MARGIN_S = 15.0
MAX_RECYCLING_ATTEMPTS = 3
MAX_LOST_ATTEMPTS = 2
_CONNECT_TIMEOUT_S = 30.0
_JITTER_S = (0.05, 0.25)

TokenProvider = Callable[[], Awaitable[str]]


@dataclass(frozen=True)
class SandboxReply:
    outcome: SandboxOutcome
    body: bytes


class _Lost(Exception):
    """A transport loss or a 5xx without an outcome header: the instance died under the request."""


@dataclass(frozen=True)
class _Raw:
    status: int
    outcome_header: Optional[str]
    payload: bytes
    encoded: bool    # a Content-Encoding other than identity: never decoded (decompression bomb)
    overflow: bool   # the body ran past its cap


class SandboxHttpClient:
    def __init__(self, base_url: str, token_provider: TokenProvider, *, http2: bool = True,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._base_url = normalise_audience(base_url)
        self._token_provider = token_provider
        self._sleep = sleep
        # trust_env=False: a system proxy silently downgraded the S0.1 spike to HTTP/1.1.
        self._client = httpx.AsyncClient(http2=http2, trust_env=False, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, op: SandboxOp, body: bytes, limits: SandboxLimits, *,
                   kind: Optional[str] = None, ext: Optional[str] = None) -> SandboxReply:
        started = time.monotonic()
        state: dict = {"ids": [], "status": "error"}
        try:
            reply = await self._call(op, body, limits, kind, ext, state)
            state["status"] = reply.outcome.status.value
            return reply
        except SandboxUnavailable:
            state["status"] = "unavailable"
            raise
        except SandboxMisconfigured:
            state["status"] = "misconfigured"
            raise
        except Exception as exc:
            logger.error("[SandboxClient] request_id=%s op=%s unexpected %s", ",".join(state["ids"]) or "-",
                         op.value, type(exc).__name__)
            raise
        finally:
            # One line per call, bounded: at most MAX_RECYCLING_ATTEMPTS + MAX_LOST_ATTEMPTS ids.
            logger.info("[SandboxClient] request_id=%s op=%s status=%s wall_s=%.3f",
                        ",".join(state["ids"]) or "-", op.value, state["status"], time.monotonic() - started)

    async def _call(self, op: SandboxOp, body: bytes, limits: SandboxLimits, kind: Optional[str],
                    ext: Optional[str], state: dict) -> SandboxReply:
        try:
            token = await self._token_provider()
        except (SandboxUnavailable, SandboxMisconfigured):
            raise   # the provider classified and logged it
        except Exception as exc:
            logger.error("[SandboxClient] request_id=- op=%s no ID token: %s", op.value, type(exc).__name__)
            raise SandboxMisconfigured("could not obtain an ID token") from exc
        total_s = limits.wall_s + TIMEOUT_MARGIN_S
        timeout = httpx.Timeout(total_s, connect=min(total_s, _CONNECT_TIMEOUT_S))
        recycling = 0
        lost = 0
        while True:
            request_id = new_request_id()
            state["ids"].append(request_id)
            headers = {
                "Authorization": f"Bearer {token}",
                META_HEADER: encode_meta(SandboxMeta(request_id=request_id, limits=limits, kind=kind, ext=ext)),
                "Content-Type": "application/octet-stream",
                "Accept-Encoding": "identity",
            }
            try:
                # httpx timeouts are per read; the deadline for the whole attempt is ours.
                raw = await asyncio.wait_for(self._send(op, body, headers, timeout, request_id), timeout=total_s)
            except (_Lost, asyncio.TimeoutError):
                lost += 1
                if lost >= MAX_LOST_ATTEMPTS:
                    logger.warning("[SandboxClient] request_id=%s op=%s lost twice", request_id, op.value)
                    return SandboxReply(SandboxOutcome(SandboxStatus.ERROR, "sandbox_lost"), b"")
                continue

            status = raw.status
            outcome = _read_outcome(raw.outcome_header)
            if status in (401, 403):
                logger.error("[SandboxClient] request_id=%s op=%s rejected with HTTP %d: deployment mistake",
                             request_id, op.value, status)
                raise SandboxMisconfigured(f"sandbox answered HTTP {status}")
            if status in (404, 405) and outcome is None:
                logger.error("[SandboxClient] request_id=%s op=%s HTTP %d without an outcome: wrong URL",
                             request_id, op.value, status)
                raise SandboxMisconfigured(f"sandbox URL answered HTTP {status}")
            if status == 429:
                raise SandboxUnavailable("sandbox is busy (429)")
            if status == 503:
                if outcome is not None and outcome.status == SandboxStatus.RECYCLING:
                    recycling += 1
                    if recycling >= MAX_RECYCLING_ATTEMPTS:
                        raise SandboxUnavailable("sandbox kept recycling")
                    await self._sleep(random.uniform(*_JITTER_S))
                    continue
                raise SandboxUnavailable("sandbox answered 503")
            if outcome is None and status >= 500:
                # A 5xx with no outcome (an encoded front-end 502/504 included) is a transport loss.
                lost += 1
                if lost >= MAX_LOST_ATTEMPTS:
                    logger.warning("[SandboxClient] request_id=%s op=%s lost twice", request_id, op.value)
                    return SandboxReply(SandboxOutcome(SandboxStatus.ERROR, "sandbox_lost"), b"")
                continue
            if status == 413:
                # With an outcome the body is the service's capped text; without one it is a front end's HTML.
                keep = outcome is not None and not raw.encoded
                return SandboxReply(SandboxOutcome(SandboxStatus.TOO_LARGE, "request_too_large"),
                                    raw.payload if keep else b"")
            if raw.encoded or outcome is None:
                return SandboxReply(SandboxOutcome(SandboxStatus.ERROR, "malformed_envelope"), b"")
            if outcome.status == SandboxStatus.RECYCLING:
                raise SandboxUnavailable("sandbox is recycling")
            if outcome.status == SandboxStatus.UNCONFIGURED:
                raise SandboxUnavailable("sandbox is not configured")
            if outcome.status == SandboxStatus.OK:
                if status != 200:
                    return SandboxReply(SandboxOutcome(SandboxStatus.ERROR, "malformed_envelope"), b"")
                if raw.overflow:
                    return SandboxReply(SandboxOutcome(SandboxStatus.TOO_LARGE, "response_too_large"), b"")
                return SandboxReply(outcome, raw.payload)
            return SandboxReply(outcome, raw.payload[:ERROR_TEXT_CAP_BYTES])

    async def _send(self, op: SandboxOp, body: bytes, headers: dict, timeout: httpx.Timeout,
                    request_id: str) -> _Raw:
        """Raises _Lost for a transport loss, SandboxUnavailable / SandboxMisconfigured for the rest."""
        url = f"{self._base_url}/v1/{op.value}"
        try:
            async with self._client.stream("POST", url, content=body, headers=headers, timeout=timeout) as resp:
                # aiter_raw: never let httpx decode the body (gzip chains have no output bound).
                encoding = resp.headers.get("Content-Encoding", "").strip().lower()
                if encoding not in ("", "identity"):
                    return _Raw(resp.status_code, resp.headers.get(OUTCOME_HEADER), b"", encoded=True, overflow=False)
                cap = RESPONSE_CAP_BYTES if resp.status_code == 200 else ERROR_TEXT_CAP_BYTES
                chunks = []
                size = 0
                overflow = False
                async for chunk in resp.aiter_raw():
                    size += len(chunk)
                    if size > cap:
                        overflow = True
                        break
                    chunks.append(chunk)
                return _Raw(resp.status_code, resp.headers.get(OUTCOME_HEADER), b"".join(chunks),
                            encoded=False, overflow=overflow)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise SandboxUnavailable("sandbox is unreachable") from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            logger.warning("[SandboxClient] request_id=%s op=%s transport loss: %s", request_id, op.value, type(exc).__name__)
            raise _Lost() from exc
        except (httpx.UnsupportedProtocol, httpx.InvalidURL) as exc:
            # The configured URL itself is unusable: a deployment mistake.
            logger.error("[SandboxClient] request_id=%s op=%s sandbox URL unusable: %s", request_id, op.value, type(exc).__name__)
            raise SandboxMisconfigured("sandbox URL is unusable") from exc
        except httpx.HTTPError as exc:
            # Anything else httpx raises (our own request could not be encoded, a response could not be decoded):
            # we cannot say the input caused it, so it is systemic and nothing is charged to the input.
            logger.error("[SandboxClient] request_id=%s op=%s unclassified transport error: %s", request_id, op.value, type(exc).__name__)
            raise SandboxUnavailable("sandbox call failed") from exc


def _read_outcome(header: Optional[str]) -> Optional[SandboxOutcome]:
    if header is None:
        return None
    try:
        return decode_outcome(header)
    except SandboxContractError:
        return None
