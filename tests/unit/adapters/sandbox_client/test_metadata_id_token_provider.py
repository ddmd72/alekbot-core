"""MetadataIdTokenProvider: off-loop fetch, cache until exp - 5 min, token never logged."""
import base64
import json
import logging
import threading

import pytest
from google.auth.exceptions import TransportError

from src.adapters.sandbox_client.metadata_id_token_provider import MetadataIdTokenProvider
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable


def jwt(exp: float) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc({'exp': exp})}.sig"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def test_token_fetched_off_the_event_loop():
    threads = []

    def fetch(audience: str) -> str:
        threads.append((threading.current_thread(), audience))
        return jwt(10_000)

    provider = MetadataIdTokenProvider("https://sbx.example/", fetch=fetch, clock=Clock(0))
    await provider()
    assert threads[0][0] is not threading.main_thread()
    assert threads[0][1] == "https://sbx.example"  # normalised: no trailing slash


async def test_token_cached_until_five_minutes_before_exp():
    calls = []
    clock = Clock(1000)

    def fetch(audience: str) -> str:
        calls.append(audience)
        return jwt(2000)

    provider = MetadataIdTokenProvider("https://sbx.example", fetch=fetch, clock=clock)
    first = await provider()
    clock.now = 1699          # exp 2000 - 300 = 1700
    assert await provider() == first and len(calls) == 1
    clock.now = 1700
    await provider()
    assert len(calls) == 2


async def test_unreadable_exp_is_not_cached():
    calls = []
    provider = MetadataIdTokenProvider("https://sbx.example", fetch=lambda a: calls.append(a) or "opaque",
                                       clock=Clock(0))
    await provider()
    await provider()
    assert len(calls) == 2


async def test_token_never_logged(caplog):
    token = jwt(10_000)
    provider = MetadataIdTokenProvider("https://sbx.example", fetch=lambda a: token, clock=Clock(0))
    with caplog.at_level(logging.DEBUG):
        await provider()

        def boom(a):
            raise RuntimeError(f"bad token {token}")
        failing = MetadataIdTokenProvider("https://sbx.example", fetch=boom, clock=Clock(0))
        with pytest.raises(SandboxMisconfigured):
            await failing()
    assert token not in caplog.text


async def test_transport_error_is_unavailable_other_errors_misconfigured(caplog):
    def blip(a):
        raise TransportError("metadata server down")

    def broken(a):
        raise ValueError("no credentials")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SandboxUnavailable):
            await MetadataIdTokenProvider("https://sbx.example", fetch=blip, clock=Clock(0))()
        with pytest.raises(SandboxMisconfigured):
            await MetadataIdTokenProvider("https://sbx.example", fetch=broken, clock=Clock(0))()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2


def test_default_fetch_bounds_the_metadata_request_timeout(monkeypatch):
    import google.auth.transport.requests as gat
    import google.oauth2.id_token as gid
    from src.adapters.sandbox_client import metadata_id_token_provider as module

    seen = {}

    class FakeRequest:
        def __call__(self, *args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")

    monkeypatch.setattr(gat, "Request", FakeRequest)
    monkeypatch.setattr(gid, "fetch_id_token", lambda request, audience: request("http://metadata", method="GET"))
    module._fetch_from_metadata("https://sbx.example")
    assert seen["timeout"] == module.METADATA_REQUEST_TIMEOUT_S
