"""Worker path end to end: the real Gmail adapter gets 403 insufficientPermissions during an indexing task ->
needs_reconnect is set, exactly one notice is sent, a second run sends none."""
from unittest.mock import AsyncMock

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from tests.support import http_boundary_fake
from tests.unit.handlers.test_worker_handler_gmail_reconnect import _ACC, _creds, _worker

LIST = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
BODY = {"error": {"code": 403, "status": "PERMISSION_DENIED", "errors": [{"reason": "insufficientPermissions"}]}}


async def test_insufficient_scope_flags_reconnect_once(monkeypatch):
    http = http_boundary_fake.install(monkeypatch)
    http.route("GET", LIST, status=403, body=BODY)
    adapter = GmailProviderAdapter("cid", "csecret")
    stored = {"creds": _creds()}
    worker, oauth, notification = _worker(stored_creds=stored["creds"])

    async def run(**_kw):
        await adapter.list_emails(_creds())

    async def get(*_a, **_k):
        return stored["creds"]

    async def update(c):
        stored["creds"] = c
        return True

    worker._email_indexing.run_indexing_job = AsyncMock(side_effect=run)
    oauth.get.side_effect = get
    oauth.update.side_effect = update

    result, status = await worker._handle_email_indexing({"job_id": "job-0000-1111"})
    assert (status, result) == (200, {"status": "failed", "error": "gmail_reconnect_required"})
    assert stored["creds"].needs_reconnect is True
    notification.notify.assert_awaited_once()
    assert notification.notify.await_args.kwargs["account_id"] == _ACC

    await worker._handle_email_indexing({"job_id": "job-0000-1111"})
    notification.notify.assert_awaited_once()
