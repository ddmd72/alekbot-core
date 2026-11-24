"""The Cabinet's wake-up route with a pasted `curl` command and a separate secret (EXTERNAL_TASK_QUEUE_RFC §5.6). The
site is that of `test_cabinet_external_worker_wake.py`; a spying cipher shows what the service stored. The command and the
secret never come back in an answer and never reach a log line. Alex Doe is `usr-1`; the worker is woken at
`hooks.example.com`.
"""
import json
import logging

import pytest

from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.unit.web.test_cabinet_external_worker_wake import WakeSite
from tests.unit.web.test_cabinet_external_workers import BODY

SECRET = "s3cret-token"
COMMAND = """curl -X POST https://hooks.example.com/wake/abc \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"text": "hello"}'"""


class SpyCipher(AesGcmTestCipher):
    def __init__(self) -> None:
        super().__init__()
        self.plaintexts = []

    async def encrypt(self, plaintext: str, *, aad: str) -> str:
        self.plaintexts.append(plaintext)
        return await super().encrypt(plaintext, aad=aad)


@pytest.fixture
async def site():
    s = WakeSite()
    s.spy = SpyCipher()
    s.service._cipher = s.spy
    s.alex = await s.user("usr-1", "acc-1")
    _, s.worker = await s.call(s.alex, "POST", "/api/external-workers", BODY)
    s.path = f"/api/external-workers/{s.worker['worker_id']}/wake"
    return s


async def test_a_curl_body_saves_the_parsed_target_and_returns_the_host_only(site, caplog):
    with caplog.at_level(logging.DEBUG):
        answer = await site.call(site.alex, "PUT", site.path, {"curl": COMMAND, "secret": f" {SECRET} "})
        _, listing = await site.call(site.alex, "GET", "/api/external-workers")
    assert answer == (200, {"wake_host": "hooks.example.com"})
    assert json.loads(site.spy.plaintexts[0]) == {
        "url": "https://hooks.example.com/wake/abc", "headers": [["Authorization", f"Bearer {SECRET}"]]}
    assert listing["workers"][0]["wake_host"] == "hooks.example.com"
    shown = "".join(site.bodies) + caplog.text
    for hidden in (SECRET, "/wake/abc", "Bearer", "hello", "curl -X"):
        assert hidden not in shown, hidden
    assert site.webhook.sent == []


async def test_the_secret_is_optional_when_the_command_holds_it(site):
    command = "curl https://hooks.example.com/wake -H 'X-Token: inline-token'"
    assert await site.call(site.alex, "PUT", site.path, {"curl": command}) == (200, {"wake_host": "hooks.example.com"})
    assert await site.call(site.alex, "PUT", site.path, {"curl": command, "secret": None}) == (
        200, {"wake_host": "hooks.example.com"})


async def test_a_refused_command_is_400_with_its_code_and_the_secret_is_not_echoed(site, caplog):
    with caplog.at_level(logging.DEBUG):
        results = [
            await site.call(site.alex, "PUT", site.path, {"curl": COMMAND, "secret": ""}),
            await site.call(site.alex, "PUT", site.path, {"curl": "curl -k https://hooks.example.com/", "secret": SECRET}),
            await site.call(site.alex, "PUT", site.path, {"curl": "curl https://hooks.example.com/", "secret": SECRET}),
        ]
    assert results == [(400, {"error": "wake_curl_secret_missing"}), (400, {"error": "wake_curl_option"}),
                       (400, {"error": "wake_curl_secret_unused"})]
    assert SECRET not in caplog.text
    _, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert data["workers"][0]["wake_host"] is None


@pytest.mark.parametrize("command, code", [
    ("curl https://10.0.0.5/ -H 'X-A: 1'", "wake_name"),
    ("curl http://hooks.example.com/", "wake_scheme"),
    ("curl https://hooks.example.com:8080/", "wake_port"),
    ("curl https://svc.example.org/", "wake_own_host"),
    ("curl https://metadata.google.internal/", "wake_internal_name"),
    ("curl https://hooks.example.com/ -H 'Host: other.example.com'", "wake_header"),
])
async def test_a_guard_level_refusal_maps_as_before(site, command, code):
    assert await site.call(site.alex, "PUT", site.path, {"curl": command}) == (400, {"error": code})


async def test_the_old_url_and_headers_body_still_works(site):
    assert await site.call(site.alex, "PUT", site.path, {
        "url": "https://hooks.example.com/wake", "headers": "X-Lane: research"}) == (
        200, {"wake_host": "hooks.example.com"})
    assert json.loads(site.spy.plaintexts[0]) == {
        "url": "https://hooks.example.com/wake", "headers": [["X-Lane", "research"]]}


@pytest.mark.parametrize("body", [
    {"curl": COMMAND, "url": "https://hooks.example.com/"},
    {"curl": COMMAND, "headers": "X-A: 1"},
    {"url": "https://hooks.example.com/", "secret": SECRET},
    {"secret": SECRET},
    {"curl": 5}, {"curl": COMMAND, "secret": 5}, {"curl": None}, {}, None,
])
async def test_a_body_of_both_shapes_or_neither_is_invalid_body(site, body):
    assert await site.call(site.alex, "PUT", site.path, body) == (400, {"error": "invalid_body"})
