"""The offline guard of the unit suite (TEST_COVERAGE_SYSTEMIC_RFC H3).

Two layers, because there are two ways out of the process:

- Python sockets: `pytest_socket.socket_allow_hosts` lets `connect()` reach loopback and unix sockets only. Creating and
  binding sockets stays allowed (every asyncio loop needs a `socketpair`).
- gRPC: Firestore and other google-cloud clients talk through gRPC's own C-core sockets, which the socket guard never
  sees. `google.auth.default` and `grpc.secure_channel` / `grpc.aio.secure_channel` raise unless the target is loopback.

A failing test is told what to do instead: a fake (tests/support/, the H1 Firestore fake), or move the test to
tests/integration/. Out of scope, by decision: subprocess children (drive parser, sandbox engine, ffmpeg).

`install(monkeypatch)` is the single entry point; tests/unit/conftest.py applies it to every unit test and
tests/plugins/network_guard.py to the offline G2 run. The socket half is undone by pytest-socket's own teardown.
"""

import re
from typing import Any

import socket
import urllib.request

import grpc
import grpc.aio
import pytest_socket

LOOPBACK_HOSTS = ["127.0.0.1", "::1"]
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_RESOLVER_PREFIX = re.compile(r"^(?:dns:(?://[^/]*/)?|ipv4:|ipv6:|passthrough:///?)", re.IGNORECASE)

HOW_TO_FIX = (
    "The unit suite is offline (tests/support/network_guard.py, TEST_COVERAGE_SYSTEMIC_RFC H3). Use a fake "
    "(tests/support/ — the Firestore fake, the in-memory repositories) or a loopback server; a test that needs the "
    "real service belongs in tests/integration/, not tests/unit/."
)


class NetworkGuardError(RuntimeError):
    """Raised by the gRPC / credentials tripwire (the socket guard raises pytest-socket's own error)."""


def target_host(target: Any) -> str:
    """The host part of a gRPC target: `dns:///h:443`, `ipv4:1.2.3.4:80`, `[::1]:50051`, `localhost:8080`, `h`."""
    text = target.decode() if isinstance(target, bytes) else str(target)
    text = _RESOLVER_PREFIX.sub("", text.strip())
    if text.startswith("["):  # [::1]:50051
        return text[1 : text.find("]")] if "]" in text else text[1:]
    if text.count(":") > 1:  # a bare IPv6 address with no port
        return text
    return text.rsplit(":", 1)[0] if ":" in text else text


def is_loopback_target(target: Any) -> bool:
    if isinstance(target, (bytes, str)) and str(target).lower().startswith(("unix:", "unix-abstract:")):
        return True
    return target_host(target).lower() in _LOOPBACK_NAMES


def _tripwire(name: str, original):
    def guarded(target, *args, **kwargs):
        if not is_loopback_target(target):
            raise NetworkGuardError(f"{name}({target!r}) reaches a non-loopback host. {HOW_TO_FIX}")
        return original(target, *args, **kwargs)

    guarded.__name__ = getattr(original, "__name__", name)
    return guarded


def _no_default_credentials(*_args, **_kwargs):
    raise NetworkGuardError(f"google.auth.default() looks for real credentials (and then the metadata server). {HOW_TO_FIX}")


PROXY_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "GRPC_PROXY", "http_proxy", "https_proxy", "all_proxy", "grpc_proxy",
)
# Where urllib.request.getproxies() looks when no proxy variable is set: the macOS system proxy (scutil --proxy, via
# _scproxy) and the Windows registry. requests, httpx and aiohttp bind `getproxies` by name at import, but its body
# looks these up in urllib.request at call time, so replacing them here reaches every client.
_SYSTEM_PROXY_LOOKUPS = ("getproxies_macosx_sysconf", "getproxies_registry")


def _no_system_proxy(monkeypatch) -> None:
    """A local intercepting proxy set as the system proxy listens on loopback, the one address the socket guard allows.
    Deleting the variables is not enough: urllib then falls back to the system setting, and a test that rewrites
    os.environ (prod_env.seal()) drops any NO_PROXY the caller set. So the system lookup itself reports no proxy."""
    for name in _SYSTEM_PROXY_LOOKUPS:
        if hasattr(urllib.request, name):
            monkeypatch.setattr(urllib.request, name, dict)


def _guard_connect_ex(monkeypatch) -> None:
    """pytest-socket patches `connect` only; `connect_ex` reaches the same place and gets the same host rule."""
    original = socket.socket.connect_ex

    def guarded(self, address):
        host = pytest_socket.host_from_connect_args((address,))
        if self.family == getattr(socket, "AF_UNIX", None) or host in LOOPBACK_HOSTS:
            return original(self, address)
        self.close()
        raise pytest_socket.SocketConnectBlockedError(LOOPBACK_HOSTS, host)

    monkeypatch.setattr(socket.socket, "connect_ex", guarded)


def install(monkeypatch) -> None:
    """Block non-loopback `connect()`/`connect_ex()` and arm the gRPC / credentials tripwire for the current test."""
    import google.auth

    # A proxy (a variable: cloud sessions set HTTPS_PROXY to a loopback port; or the system setting: a local intercepting
    # proxy on a Mac) would let requests/httpx/aiohttp/urllib tunnel out through the one address the socket guard allows.
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    _no_system_proxy(monkeypatch)
    pytest_socket.socket_allow_hosts(LOOPBACK_HOSTS, allow_unix_socket=True)
    _guard_connect_ex(monkeypatch)
    monkeypatch.setattr(google.auth, "default", _no_default_credentials)
    monkeypatch.setattr(grpc, "secure_channel", _tripwire("grpc.secure_channel", grpc.secure_channel))
    monkeypatch.setattr(grpc.aio, "secure_channel", _tripwire("grpc.aio.secure_channel", grpc.aio.secure_channel))
