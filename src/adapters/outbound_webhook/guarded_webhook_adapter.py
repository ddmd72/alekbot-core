"""GuardedWebhookAdapter — OutboundWebhookPort with the outbound-request guard (EXTERNAL_TASK_QUEUE_RFC §5.8, §6.2).

One send, in order, all inside `WAKE_TIMEOUT_S` (DNS included):
1. The name-level rules of `src/domain/wake_target.py` again (the Cabinet ran them at save time; DNS and the bot's own
   hosts may have changed since): `https`, port 443, no userinfo, a DNS name of two labels or more, not internal, not
   one of `own_hosts`, not under `run.app`; header names, values, count and size.
2. The name is resolved asynchronously; EVERY resolved address must be public (`address_is_public`).
3. The connection goes to the first vetted address, not to the name: the request URL carries the address literal,
   while `Host` and the TLS SNI (and so certificate verification) carry the name. httpx is never handed the name to
   resolve again, so a second DNS answer cannot slip in between the check and the connect (rebinding).
4. `follow_redirects=False` (a 3xx is `redirect`), `trust_env=False` (no proxy, no netrc from the environment).
5. The answer's body is asked for uncompressed (`Accept-Encoding: identity`) and read as raw bytes, never decompressed,
   up to `WAKE_RESPONSE_READ_MAX` bytes, then dropped: a compressed body cannot expand past the cap.

Outcomes: None for 2xx, else a `WakeRefusal`. Logs: `[Wake] host=<name> outcome=<code>` and, on an exception,
`error=<class name>`; never the URL, a path, a query, a header, a status reason phrase or a body. httpx logs every request
with its full URL at INFO: entry points set its logger to WARNING (`quiet_third_party_loggers`), and a filter installed
here drops every `httpx` record while a send runs, whatever the level, so a query-string token never reaches a log.

Egress assumption: the main service has no VPC egress, so the only network a send reaches is the public internet. The
address rules do not know a private network's own ranges or names; a VPC connector on the main service reopens this
(docs/07_deployment/README.md, the egress trap).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import socket
from typing import Any, Awaitable, Callable, Collection, List, Mapping, Optional, Sequence

import httpx

from ...domain.external_task import WAKE_RESPONSE_READ_MAX, WAKE_TIMEOUT_S
from ...domain.wake_refusal import WakeRefusal
from ...domain.wake_target import CheckedTarget, WakeTarget, address_is_public, check_target
from ...ports.outbound_webhook_port import OutboundWebhookPort
from ...utils.logger import logger

Resolver = Callable[[str], Awaitable[Sequence[str]]]

_HTTPS_PORT = 443
_USER_AGENT = "alek-wake/1"
_SENDING: contextvars.ContextVar[bool] = contextvars.ContextVar("outbound_webhook_sending", default=False)


class _DropWhileSending(logging.Filter):
    """Drops the `httpx` logger's records (they carry the request URL) raised inside a send."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _SENDING.get()


logging.getLogger("httpx").addFilter(_DropWhileSending())


async def resolve_host(host: str) -> List[str]:
    """Every address the system resolver gives for `host`, in its order, without repeats."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, _HTTPS_PORT, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    addresses: List[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def build_client(transport: Optional[httpx.AsyncBaseTransport], timeout_s: float) -> httpx.AsyncClient:
    """The client of one send: no redirects followed, nothing taken from the environment (proxies, netrc)."""
    return httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False,
                             timeout=httpx.Timeout(timeout_s))


def _status_refusal(status: int) -> Optional[WakeRefusal]:
    if 200 <= status < 300:
        return None
    if 300 <= status < 400:
        return WakeRefusal.REDIRECT
    if status >= 500:
        return WakeRefusal.SERVER_ERROR
    return WakeRefusal.CLIENT_ERROR


class GuardedWebhookAdapter(OutboundWebhookPort):
    """`own_hosts`: the bot's own host names, from every deploy unit's configured URL (composition builds it).
    `resolve` and `transport` are the DNS and HTTP seams; production uses the system resolver and httpx's own."""

    def __init__(self, own_hosts: Collection[str], *, resolve: Resolver = resolve_host,
                 transport: Optional[httpx.AsyncBaseTransport] = None, timeout_s: float = WAKE_TIMEOUT_S,
                 read_max: int = WAKE_RESPONSE_READ_MAX) -> None:
        self._own_hosts = frozenset(h.lower() for h in own_hosts)
        self._resolve = resolve
        self._transport = transport
        self._timeout_s = timeout_s
        self._read_max = read_max

    async def post(self, target: WakeTarget, body: Mapping[str, Any]) -> Optional[WakeRefusal]:
        checked = check_target(target, self._own_hosts)
        if isinstance(checked, WakeRefusal):
            logger.warning(f"[Wake] host=- outcome={checked.value}")
            return checked
        try:
            refusal = await asyncio.wait_for(self._send(checked, target, body), self._timeout_s)
        except asyncio.TimeoutError:
            logger.warning(f"[Wake] host={checked.host} error=TimeoutError")
            refusal = WakeRefusal.UNREACHABLE
        logger.info(f"[Wake] host={checked.host} outcome={refusal.value if refusal else 'delivered'}")
        return refusal

    async def _send(self, checked: CheckedTarget, target: WakeTarget,
                    body: Mapping[str, Any]) -> Optional[WakeRefusal]:
        # Since Python 3.12 `asyncio.wait_for` runs this coroutine in the caller's own task, so the flag is reset by
        # its token on every way out: an httpx record the caller logs after the send is never dropped.
        token = _SENDING.set(True)
        try:
            return await self._send_flagged(checked, target, body)
        finally:
            _SENDING.reset(token)

    async def _send_flagged(self, checked: CheckedTarget, target: WakeTarget,
                            body: Mapping[str, Any]) -> Optional[WakeRefusal]:
        try:
            addresses = list(await self._resolve(checked.host))
        except (OSError, UnicodeError) as exc:      # socket.gaierror is an OSError
            logger.warning(f"[Wake] host={checked.host} error={type(exc).__name__}")
            return WakeRefusal.DNS_FAILURE
        if not addresses:
            return WakeRefusal.DNS_FAILURE
        if not all(address_is_public(a) for a in addresses):
            return WakeRefusal.ADDRESS
        address = addresses[0]
        literal = f"[{address}]" if ":" in address else address
        # `identity`: the answer is counted in bytes as they arrive, so a compressed body cannot expand past the cap.
        headers = [*target.headers, ("Host", checked.host), ("Content-Type", "application/json"),
                   ("Accept-Encoding", "identity"), ("User-Agent", _USER_AGENT)]
        content = json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
        async with build_client(self._transport, self._timeout_s) as client:
            try:
                async with client.stream("POST", f"https://{literal}{checked.request_target}", headers=headers,
                                         content=content, extensions={"sni_hostname": checked.host}) as response:
                    status = response.status_code
                    await self._drain(response)
            except httpx.HTTPError as exc:
                logger.warning(f"[Wake] host={checked.host} error={type(exc).__name__}")
                return WakeRefusal.UNREACHABLE
        return _status_refusal(status)

    async def _drain(self, response: httpx.Response) -> None:
        """Read the answer's raw bytes (never decompressed) up to the cap, then stop; nothing is kept. A response
        already held in memory (no stream left to read) has nothing to drain."""
        if response.is_stream_consumed:
            return
        read = 0
        async for chunk in response.aiter_raw():
            read += len(chunk)
            if read >= self._read_max:
                return
