"""GuardedWebhookAdapter: the outbound-request guard at send time (EXTERNAL_TASK_QUEUE_RFC §5.8, §6.2).

Each rule has its own test. The DNS seam is a recording resolver; the HTTP seam is httpx's `MockTransport`, which sees
exactly the request httpx would put on the wire: the URL it connects to, the headers and the TLS SNI extension. The
bot's own host is `svc.example.org`; Alex Doe's worker is woken at `hooks.example.com`.
"""
import asyncio
import logging
import socket
from typing import List, Sequence

import httpx
import pytest

from src.adapters.outbound_webhook.guarded_webhook_adapter import (
    GuardedWebhookAdapter,
    build_client,
    resolve_host,
)
from src.domain.wake_refusal import WakeRefusal
from src.domain.wake_target import WakeTarget

R = WakeRefusal
PUBLIC = "93.184.216.34"
TARGET = WakeTarget("https://hooks.example.com/wake?token=s3cret-query", (("Authorization", "Bearer s3cret-header"),))
BODY = {"event": "tasks_available", "worker": "wrk-1"}


class Resolver:
    def __init__(self, *answers: Sequence[str], error: Exception = None, delay: float = 0.0) -> None:
        self.answers: List[Sequence[str]] = list(answers)
        self.error = error
        self.delay = delay
        self.asked: List[str] = []

    async def __call__(self, host: str) -> Sequence[str]:
        self.asked.append(host)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Target:
    """The other end: records every request and answers `status` (with `body`)."""

    def __init__(self, status: int = 204, body: bytes = b"", headers: dict = None, error: Exception = None) -> None:
        self.requests: List[httpx.Request] = []
        self.status, self.body, self.headers, self.error = status, body, headers or {}, error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, headers=self.headers, content=self.body)


def adapter(resolver: Resolver, target: Target, **kw) -> GuardedWebhookAdapter:
    return GuardedWebhookAdapter({"SVC.example.org"}, resolve=resolver, transport=httpx.MockTransport(target), **kw)


async def test_a_public_target_gets_one_post_on_the_vetted_address_with_the_name_as_host_and_sni():
    resolver, target = Resolver([PUBLIC]), Target(204)
    assert await adapter(resolver, target).post(TARGET, BODY) is None
    [request] = target.requests
    assert request.method == "POST" and request.url.host == PUBLIC and request.url.port is None
    assert request.url.raw_path == b"/wake?token=s3cret-query"
    assert request.headers["host"] == "hooks.example.com"
    assert request.extensions["sni_hostname"] == "hooks.example.com"
    assert request.headers["authorization"] == "Bearer s3cret-header"
    assert request.headers["content-type"] == "application/json"
    assert request.content == b'{"event":"tasks_available","worker":"wrk-1"}'
    assert resolver.asked == ["hooks.example.com"]


async def test_an_ipv6_answer_is_connected_to_as_a_bracketed_literal():
    target = Target(200)
    assert await adapter(Resolver(["2606:4700::6810:84e5"]), target).post(TARGET, BODY) is None
    assert target.requests[0].url.host == "2606:4700::6810:84e5"


async def test_the_name_is_resolved_once_so_a_second_dns_answer_cannot_be_used(monkeypatch):
    """Rebinding: the first answer is public, any later one private. Only the first is ever asked for, and httpx is
    never handed the name (the URL carries the address), so nothing resolves it again."""
    resolver, target = Resolver([PUBLIC], ["10.0.0.5"]), Target(204)

    async def no_system_dns(*args, **kwargs):
        raise AssertionError("the system resolver was asked")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", no_system_dns)
    assert await adapter(resolver, target).post(TARGET, BODY) is None
    assert resolver.asked == ["hooks.example.com"] and target.requests[0].url.host == PUBLIC


@pytest.mark.parametrize("answer", [["10.0.0.5"], ["169.254.169.254"], ["100.64.0.1"], ["::1"], ["fd00::1"],
                                    ["64:ff9b::a00:5"], ["2002:a00:5::1"], ["2001:0:4136:e378::1"],
                                    ["::ffff:127.0.0.1"], [PUBLIC, "10.0.0.5"]])
async def test_every_resolved_address_must_be_public(answer):
    target = Target(204)
    assert await adapter(Resolver(answer), target).post(TARGET, BODY) is R.ADDRESS
    assert target.requests == []


async def test_a_name_that_does_not_resolve_is_a_dns_failure():
    target = Target(204)
    gaierror = socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
    assert await adapter(Resolver(error=gaierror), target).post(TARGET, BODY) is R.DNS_FAILURE
    assert await adapter(Resolver([]), target).post(TARGET, BODY) is R.DNS_FAILURE
    assert target.requests == []


@pytest.mark.parametrize("url, code", [
    ("http://hooks.example.com/", R.SCHEME), ("https://hooks.example.com:8443/", R.PORT),
    ("https://203.0.113.7/", R.NAME), ("https://[2606:4700::1]/", R.NAME), ("https://localhost/", R.NAME),
    ("https://metadata.google.internal/", R.INTERNAL_NAME), ("https://svc.example.org/worker", R.OWN_HOST),
    ("https://alek-abc.a.run.app/", R.OWN_HOST),
])
async def test_the_name_rules_run_again_at_send_time_without_any_dns_query(url, code):
    resolver, target = Resolver([PUBLIC]), Target(204)
    assert await adapter(resolver, target).post(WakeTarget(url), BODY) is code
    assert resolver.asked == [] and target.requests == []


async def test_a_framing_header_is_refused_again_at_send_time():
    resolver, target = Resolver([PUBLIC]), Target(204)
    bad = WakeTarget("https://hooks.example.com/", (("Transfer-Encoding", "chunked"),))
    assert await adapter(resolver, target).post(bad, BODY) is R.HEADER
    assert resolver.asked == [] and target.requests == []


async def test_a_redirect_is_not_followed():
    target = Target(302, headers={"Location": "http://169.254.169.254/computeMetadata/v1/"})
    assert await adapter(Resolver([PUBLIC]), target).post(TARGET, BODY) is R.REDIRECT
    assert len(target.requests) == 1


@pytest.mark.parametrize("status, code", [(400, R.CLIENT_ERROR), (401, R.CLIENT_ERROR), (404, R.CLIENT_ERROR),
                                          (101, R.CLIENT_ERROR), (500, R.SERVER_ERROR), (503, R.SERVER_ERROR),
                                          (200, None), (202, None)])
async def test_the_answer_status_maps_to_a_closed_code(status, code):
    assert await adapter(Resolver([PUBLIC]), Target(status)).post(TARGET, BODY) is code


async def test_a_connection_failure_is_unreachable():
    target = Target(error=httpx.ConnectError("connection refused"))
    assert await adapter(Resolver([PUBLIC]), target).post(TARGET, BODY) is R.UNREACHABLE


async def test_a_send_that_outruns_the_timeout_dns_included_is_unreachable():
    resolver = Resolver([PUBLIC], delay=5)
    assert await adapter(resolver, Target(204), timeout_s=0.05).post(TARGET, BODY) is R.UNREACHABLE


async def test_the_answer_body_is_read_up_to_the_cap_and_dropped():
    pulled: List[int] = []

    async def chunks():
        for i in range(1000):
            pulled.append(i)
            yield b"x" * 100

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    hook = GuardedWebhookAdapter({"svc.example.org"}, resolve=Resolver([PUBLIC]),
                                 transport=httpx.MockTransport(answer), read_max=1_000)
    assert await hook.post(TARGET, BODY) is None
    assert len(pulled) <= 11


def test_the_client_follows_no_redirect_and_takes_no_proxy_from_the_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.net:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.net:3128")
    client = build_client(None, 5)
    assert client.follow_redirects is False and client.trust_env is False
    assert client._mounts == {}


async def test_no_log_line_carries_the_url_a_header_or_the_body(caplog):
    hook = adapter(Resolver([PUBLIC]), Target(500, body=b"secret answer body"))
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        try:
            await hook.post(TARGET, BODY)
            await adapter(Resolver(error=socket.gaierror("x")), Target()).post(TARGET, BODY)
            await adapter(Resolver([PUBLIC]), Target(error=httpx.ConnectError("to /wake?token=s3cret-query"))).post(
                TARGET, BODY)
            await adapter(Resolver([PUBLIC]), Target()).post(WakeTarget("http://hooks.example.com/w?k=s3cret"), BODY)
        finally:
            logging.getLogger("httpx").setLevel(logging.WARNING)
    text = caplog.text
    assert "hooks.example.com" in text
    for leaked in ("s3cret", "/wake", "Bearer", "secret answer body", "tasks_available"):
        assert leaked not in text, leaked


async def test_the_system_resolver_returns_each_address_once():
    addresses = await resolve_host("localhost")
    assert addresses and len(addresses) == len(set(addresses))
    assert set(addresses) <= {"127.0.0.1", "::1"}


async def test_the_system_resolver_drops_a_repeated_address(monkeypatch):
    async def answers(host, port, **kwargs):
        assert (host, port) == ("hooks.example.com", 443)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::6810:84e5", 443, 0, 0))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", answers)
    assert await resolve_host("hooks.example.com") == [PUBLIC, "2606:4700::6810:84e5"]
