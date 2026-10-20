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


class _Resp:
    def __init__(self, status=200, data=b"", content_type="application/json"):
        self.status = status
        self.data = data
        self.headers = {"metadata-flavor": "Google", "content-type": content_type}


def _metadata_request(identity):
    """A fake google-auth Request: ping and service-account info succeed, `identity()` answers the identity endpoint."""
    def request(url=None, method="GET", **kwargs):
        if "identity" in url:
            return identity()
        if "service-accounts" in url:
            return _Resp(data=json.dumps({"email": "sa@example.test", "aliases": ["default"], "scopes": []}).encode())
        return _Resp()
    return request


def _real_fetch(request):
    import google.oauth2.id_token as id_token

    def fetch(audience):
        return id_token.fetch_id_token(request, audience)
    return fetch


@pytest.fixture(autouse=True)
def _no_metadata_retry_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("K_JOB", raising=False)


async def _fetch_with(request, caplog):
    provider = MetadataIdTokenProvider("https://sbx.example", fetch=_real_fetch(request), clock=Clock(0))
    with caplog.at_level(logging.ERROR):
        await provider()


def _blip():
    raise TransportError("metadata server down")


async def test_real_fetch_ping_blip_on_cloud_run_is_unavailable(monkeypatch, caplog):
    monkeypatch.setenv("K_SERVICE", "svc")
    with pytest.raises(SandboxUnavailable):
        await _fetch_with(lambda *a, **k: _blip(), caplog)   # ping swallowed -> DefaultCredentialsError
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


async def test_real_fetch_ping_failure_off_cloud_run_is_misconfigured(caplog):
    with pytest.raises(SandboxMisconfigured):
        await _fetch_with(lambda *a, **k: _blip(), caplog)   # no credentials anywhere


async def test_real_fetch_identity_endpoint_blip_is_unavailable(caplog):
    with pytest.raises(SandboxUnavailable):
        await _fetch_with(_metadata_request(_blip), caplog)  # RefreshError caused by TransportError


async def test_real_fetch_genuine_non_transport_error_is_misconfigured(monkeypatch, caplog):
    monkeypatch.setenv("K_SERVICE", "svc")
    with pytest.raises(SandboxMisconfigured):
        # the endpoint answers 200 with something that is not a JWT: google-auth raises MalformedError
        await _fetch_with(_metadata_request(lambda: _Resp(200, b"not-a-jwt", "text/plain")), caplog)


async def test_real_fetch_success_returns_the_token():
    token = jwt(10_000)
    provider = MetadataIdTokenProvider(
        "https://sbx.example", clock=Clock(0),
        fetch=_real_fetch(_metadata_request(lambda: _Resp(200, token.encode(), "text/plain"))))
    assert await provider() == token


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
