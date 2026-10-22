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


def install(monkeypatch) -> None:
    """Block non-loopback `connect()` and arm the gRPC / credentials tripwire for the current test."""
    import google.auth

    pytest_socket.socket_allow_hosts(LOOPBACK_HOSTS, allow_unix_socket=True)
    monkeypatch.setattr(google.auth, "default", _no_default_credentials)
    monkeypatch.setattr(grpc, "secure_channel", _tripwire("grpc.secure_channel", grpc.secure_channel))
    monkeypatch.setattr(grpc.aio, "secure_channel", _tripwire("grpc.aio.secure_channel", grpc.aio.secure_channel))
