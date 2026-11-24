"""The Cabinet's wake-up routes of a worker (EXTERNAL_TASK_QUEUE_RFC §5.6, §5.8): set and clear the target.

The site of `test_cabinet_external_workers.py`, its service rebuilt with the test cipher and a recording webhook (the
save runs no DNS and sends nothing). No answer and no log line carries the URL's path or query or a header value; after
a save the page gets the host only. Alex Doe is `usr-1`; Alex's worker is woken at `hooks.example.com`.
"""
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.services.external_task_service import ExternalTaskService
from src.services.inactive_user_check import InactiveUserCheck
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.unit.services.test_external_task_wake import RecordingWebhook
from tests.unit.web.test_cabinet_external_workers import BODY, Site

URL = "https://hooks.example.com/wake?token=s3cret-query"
LINES = "Authorization: Bearer s3cret-header\nX-Lane: research"


class WakeSite(Site):
    def __init__(self, cipher: bool = True) -> None:
        super().__init__()
        self.webhook = RecordingWebhook()
        self.service = ExternalTaskService(
            self.tasks, self.workers, self.queue, self.users, InactiveUserCheck(self.users),
            digest_key=bytes(range(32)), clock=lambda: datetime.now(timezone.utc), new_id=self._new_id,
            random_bytes=self._random_bytes, cipher=AesGcmTestCipher() if cipher else None, webhook=self.webhook,
            own_hosts=("svc.example.org",))
        self.app = Quart("workers-wake")
        self.app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), cabinet_sessions=self.kit.verifier, user_repo=MagicMock(),
            fact_repo=MagicMock(), embedding_service=MagicMock(), external_tasks=self.service))
        self.http = self.app.test_client()


@pytest.fixture
async def site():
    s = WakeSite()
    s.alex = await s.user("usr-1", "acc-1")
    s.other = await s.user("usr-2", "acc-2")
    _, s.worker = await s.call(s.alex, "POST", "/api/external-workers", BODY)
    s.path = f"/api/external-workers/{s.worker['worker_id']}/wake"
    return s


async def test_a_saved_target_comes_back_as_its_host_only(site, caplog):
    with caplog.at_level(logging.DEBUG):
        assert await site.call(site.alex, "PUT", site.path, {"url": URL, "headers": LINES}) == (
            200, {"wake_host": "hooks.example.com"})
        status, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert status == 200 and data["workers"][0]["wake_host"] == "hooks.example.com"
    sent = "".join(site.bodies)
    for secret in ("s3cret", "/wake", "Bearer", "X-Lane"):
        assert secret not in sent and secret not in caplog.text, secret
    assert site.webhook.sent == []


async def test_a_new_worker_polls_and_headers_are_optional(site):
    _, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert data["workers"][0]["wake_host"] is None
    assert await site.call(site.alex, "PUT", site.path, {"url": "https://hooks.example.com/"}) == (
        200, {"wake_host": "hooks.example.com"})
    assert await site.call(site.alex, "PUT", site.path, {"url": "https://relay.example.net/", "headers": None}) == (
        200, {"wake_host": "relay.example.net"})


@pytest.mark.parametrize("url, code", [
    ("http://hooks.example.com/", "wake_scheme"), ("https://hooks.example.com:8080/", "wake_port"),
    ("https://10.0.0.5/", "wake_name"), ("https://metadata.google.internal/", "wake_internal_name"),
    ("https://svc.example.org/", "wake_own_host"), ("https://worker-abc.a.run.app/", "wake_own_host"),
])
async def test_a_refused_target_is_400_with_its_code_and_nothing_is_stored(site, url, code):
    assert await site.call(site.alex, "PUT", site.path, {"url": url}) == (400, {"error": code})
    _, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert data["workers"][0]["wake_host"] is None


@pytest.mark.parametrize("lines", ["no colon", "Host: elsewhere.example", "Content-Length: 5"])
async def test_a_bad_header_line_is_wake_header(site, lines):
    assert await site.call(site.alex, "PUT", site.path, {"url": URL, "headers": lines}) == (
        400, {"error": "wake_header"})


@pytest.mark.parametrize("body", [None, {}, {"url": 5}, {"url": URL, "headers": ["Authorization: x"]}])
async def test_a_malformed_body_is_invalid_body(site, body):
    assert await site.call(site.alex, "PUT", site.path, body) == (400, {"error": "invalid_body"})


async def test_clearing_makes_the_worker_poll(site):
    await site.call(site.alex, "PUT", site.path, {"url": URL})
    assert await site.call(site.alex, "DELETE", site.path) == (200, {"wake_host": None})
    _, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert data["workers"][0]["wake_host"] is None


async def test_another_users_worker_and_a_lock_are_refused(site):
    assert await site.call(site.other, "PUT", site.path, {"url": URL}) == (404, {"error": "not_found"})
    assert await site.call(site.other, "DELETE", site.path) == (404, {"error": "not_found"})
    await site.lock("usr-1")
    assert await site.call(site.alex, "PUT", site.path, {"url": URL}) == (423, {"error": "account_locked"})


async def test_a_server_without_the_cipher_or_the_service_cannot_store_a_target():
    bare = WakeSite(cipher=False)
    auth = await bare.user("usr-1", "acc-1")
    _, worker = await bare.call(auth, "POST", "/api/external-workers", BODY)
    path = f"/api/external-workers/{worker['worker_id']}/wake"
    assert await bare.call(auth, "PUT", path, {"url": URL}) == (400, {"error": "wake_unavailable"})
    none = Site(with_service=False)
    auth = await none.user("usr-1", "acc-1")
    assert await none.call(auth, "PUT", "/api/external-workers/wrk-1/wake", {"url": URL}) == (
        404, {"error": "not_found"})
    assert await none.call(auth, "DELETE", "/api/external-workers/wrk-1/wake") == (404, {"error": "not_found"})
