"""A worker's wake-up target and the guard's pure rules (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.8 "Outbound-request guard").

One module on purpose: the target, its checked form and the rules that turn one into the other are one contract shared
by the Cabinet save (`ExternalTaskService.save_wake_target`, the name-level rules only) and the sending adapter
(`src/adapters/outbound_webhook/`, the same rules again, then DNS and the addresses).

Public interface:
    wake_aad(user_id, worker_id) -> str                         AAD of the target's ciphertext (E3)
    WakeTarget(url, headers)                                    .to_json() / WakeTarget.from_json(text)
    CheckedTarget(host, request_target)                         what the name-level rules let through
    check_url(url, own_hosts) -> CheckedTarget | WakeRefusal    scheme, port, userinfo, name, internal, own host
    check_headers(headers) -> WakeRefusal | None                names, values, framing headers, count and size
    check_target(target, own_hosts) -> CheckedTarget | WakeRefusal
    parse_header_lines(text) -> tuple of (name, value) | WakeRefusal     the Cabinet's `Name: value` lines
    address_is_public(address) -> bool                          one resolved address
    own_hosts_of(urls) -> frozenset of host names               the bot's own hosts, from its configured URLs

Why the rules sit before any resolution: a name that is an IP literal, has one label, ends in an internal suffix or is
one of the bot's own hosts is refused without a DNS query, so a target can never be aimed inward by name. The address
rule then covers what a public-looking name resolves to. Every refusal is a closed `WakeRefusal` code.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Collection, FrozenSet, Iterable, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from .external_task import WAKE_HEADER_BYTES_MAX, WAKE_HEADERS_MAX, WAKE_URL_MAX_CHARS
from .wake_refusal import WakeRefusal

Headers = Tuple[Tuple[str, str], ...]

_AAD_STORE = "external_workers"
_HTTPS_PORT = 443
# Names that only ever mean a private network: the platform's metadata name sits under `.internal`.
_INTERNAL_SUFFIXES = frozenset({"internal", "localhost", "local", "arpa"})
# The platform's default service domain: every name under it is refused, since a bot's service may sit there.
_PLATFORM_DOMAIN = "run.app"
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NAME_MAX_CHARS = 253
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_VALUE = re.compile(r"[\x20-\x7e\t]*")
# Hop-by-hop and framing headers: the transport sets them, a user value would change how the request is framed.
# `accept-encoding` too: the sender asks for an uncompressed answer so its read cap counts what arrives.
_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection", "keep-alive",
                                "proxy-connection", "te", "trailer", "upgrade", "content-type", "accept-encoding"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# IPv6 ranges that carry an IPv4 address inside: what they reach is decided by a translator, not by this check.
_EMBEDS_IPV4 = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32"))


def wake_aad(user_id: str, worker_id: str) -> str:
    """The ciphertext is bound to the worker record (E3): it decrypts under this AAD only."""
    return f"{_AAD_STORE}/{user_id}:{worker_id}"


@dataclass(frozen=True)
class WakeTarget:
    """The secret part of a worker's wake-up: the URL (encrypted too, users put tokens in query strings) and the headers
    sent with every ping."""
    url: str
    headers: Headers = ()

    def to_json(self) -> str:
        return json.dumps({"url": self.url, "headers": [list(h) for h in self.headers]}, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "WakeTarget":
        """Raises ValueError on a text that is not a stored target."""
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("url"), str):
            raise ValueError("not a wake target")
        headers = data.get("headers", [])
        if not isinstance(headers, list) or not all(
                isinstance(h, list) and len(h) == 2 and all(isinstance(x, str) for x in h) for h in headers):
            raise ValueError("not a wake target")
        return cls(url=data["url"], headers=tuple((h[0], h[1]) for h in headers))


@dataclass(frozen=True)
class CheckedTarget:
    """`host` is the lowercase ASCII DNS name (no trailing dot): it is resolved, sent as SNI and as `Host`.
    `request_target` is the path and query sent on the request line (`/` when the URL has none)."""
    host: str
    request_target: str


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _host_refusal(host: str, own_hosts: Collection[str]) -> Optional[WakeRefusal]:
    if not host or len(host) > _NAME_MAX_CHARS:
        return WakeRefusal.NAME
    if _is_ip_literal(host):
        return WakeRefusal.NAME          # an IP literal is never a name
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        return WakeRefusal.NAME          # one label, not a hostname, or a numeric form (`127.1`, `0x7f.0.0.1`)
    if labels[-1] in _INTERNAL_SUFFIXES:
        return WakeRefusal.INTERNAL_NAME
    if host in own_hosts or host == _PLATFORM_DOMAIN or host.endswith("." + _PLATFORM_DOMAIN):
        return WakeRefusal.OWN_HOST
    return None


def check_url(url: str, own_hosts: Collection[str]) -> Union[CheckedTarget, WakeRefusal]:
    """The URL rules, no I/O: `https` only, port 443 only, no userinfo, a DNS name of two labels or more that is not
    internal and not one of the bot's own hosts (`own_hosts`, lowercase) or the platform domain."""
    if (not isinstance(url, str) or not url or len(url) > WAKE_URL_MAX_CHARS or not url.isascii()
            or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in url)):
        return WakeRefusal.NAME
    try:
        parts = urlsplit(url)
    except ValueError:
        return WakeRefusal.NAME          # e.g. an unclosed bracket
    if parts.scheme.lower() != "https":
        return WakeRefusal.SCHEME
    try:
        port = parts.port
    except ValueError:
        return WakeRefusal.PORT
    if port is not None and port != _HTTPS_PORT:
        return WakeRefusal.PORT
    if "@" in parts.netloc or "[" in parts.netloc:
        return WakeRefusal.NAME          # userinfo, or a bracketed IPv6 literal
    host = (parts.hostname or "").lower()
    host = host[:-1] if host.endswith(".") else host
    refusal = _host_refusal(host, own_hosts)
    if refusal is not None:
        return refusal
    path = parts.path or "/"
    return CheckedTarget(host=host, request_target=f"{path}?{parts.query}" if parts.query else path)


def check_headers(headers: Sequence[Tuple[str, str]]) -> Optional[WakeRefusal]:
    """`header` when there are more than `WAKE_HEADERS_MAX`, names and values together exceed
    `WAKE_HEADER_BYTES_MAX`, a name is not a token, repeats or is a framing header, or a value holds a control
    character or non-ASCII text."""
    if len(headers) > WAKE_HEADERS_MAX:
        return WakeRefusal.HEADER
    seen = set()
    size = 0
    for name, value in headers:
        if not isinstance(name, str) or not isinstance(value, str):
            return WakeRefusal.HEADER
        lowered = name.lower()
        if not _TOKEN.fullmatch(name) or lowered in _FORBIDDEN_HEADERS or lowered in seen:
            return WakeRefusal.HEADER
        if not _VALUE.fullmatch(value):
            return WakeRefusal.HEADER
        seen.add(lowered)
        size += len(name) + len(value)
    return WakeRefusal.HEADER if size > WAKE_HEADER_BYTES_MAX else None


def check_target(target: WakeTarget, own_hosts: Collection[str]) -> Union[CheckedTarget, WakeRefusal]:
    """The URL rules, then the header rules."""
    checked = check_url(target.url, own_hosts)
    if isinstance(checked, WakeRefusal):
        return checked
    return check_headers(target.headers) or checked


def parse_header_lines(text: str) -> Union[Headers, WakeRefusal]:
    """`Name: value` per line (blank lines skipped) into header pairs, checked by `check_headers`. A line without a
    colon or with an empty name is `header`."""
    if not isinstance(text, str):
        return WakeRefusal.HEADER
    pairs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        name, colon, value = line.partition(":")
        if not colon or not name.strip():
            return WakeRefusal.HEADER
        pairs.append((name.strip(), value.strip()))
    headers = tuple(pairs)
    return check_headers(headers) or headers


def address_is_public(address: str) -> bool:
    """True only for a globally routable unicast address. Refused: private, loopback, link-local (the metadata address
    among them), CGNAT, multicast, reserved, unspecified and documentation ranges, IPv6 ULA, site-local (`fec0::/10`,
    which Python counts as global) and link-local, and every IPv6 range that embeds IPv4 (NAT64, 6to4, Teredo). An
    IPv4-mapped IPv6 address is judged as its IPv4 address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _EMBEDS_IPV4):
            return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
            or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)):
        return False
    return ip.is_global


def own_hosts_of(urls: Iterable[Optional[str]]) -> FrozenSet[str]:
    """The lowercase host names of the configured URLs (empty and unparseable ones skipped)."""
    hosts = set()
    for url in urls:
        if not url or not url.strip():
            continue
        try:
            host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if host:
            hosts.add(host)
    return frozenset(hosts)
