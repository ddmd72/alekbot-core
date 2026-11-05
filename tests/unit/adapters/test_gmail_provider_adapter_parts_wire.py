"""GmailProviderAdapter.list_parts / get_part - wire test at the aiohttp boundary (tests/support/http_boundary_fake).

Invented Gmail payloads in tests/fixtures/email/: a nested mixed -> alternative -> related message with an inline logo
(Content-ID + filename), two parts with one name, a forwarded .eml with a child, and a small part whose data is inline.
Pins: the requests sent, the parts listed (ids, names, handles), the bytes returned, the typed errors, the size cap.
"""
import asyncio
import base64
import json
from pathlib import Path

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.domain.email import EmailPart
from src.domain.exceptions import (
    EmailMessageGone,
    EmailPartTooLarge,
    EmailPartUnavailable,
    OAuthScopeInsufficientError,
)
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "email"
MSG = "18f0a1b2c3d4e5f6"
PDF_NAME = "invoice_INV-2024-417.pdf"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _enc(data: bytes, pad: bool = False) -> str:
    out = base64.urlsafe_b64encode(data).decode()
    return out if pad else out.rstrip("=")


@pytest.fixture
def http(monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages/{MSG}", 200, _fixture("gmail_multipart_nested.json"))
    yield fake
    assert fake.unrouted == []


@pytest.fixture
def adapter():
    return GmailProviderAdapter("cid", "csecret")


CREDS = creds("user-1", "gmail", access_token="TOKEN-1")


async def test_list_parts_walks_the_whole_tree_and_sends_one_full_get(http, adapter):
    parts = await adapter.list_parts(CREDS, MSG)

    assert [(p.part_id, p.filename, p.mime, p.size, p.handle) for p in parts] == [
        ("0.1.1", "logo.png", "image/png", 4096, "ATT-LOGO-a"),
        ("1", PDF_NAME, "application/pdf", 20000, "ATT-PDF-a"),
        ("2", PDF_NAME, "application/pdf", 21000, "ATT-PDF-b"),
        ("3", "forwarded.eml", "message/rfc822", 3000, "ATT-EML-a"),
        ("4", "totals.csv", "text/csv", 24, ""),
    ]
    (request,) = http.requests
    assert request.url == f"{BASE}/messages/{MSG}"
    assert dict(request.params) == {"format": "full"}
    assert request.headers["Authorization"] == "Bearer TOKEN-1"


async def test_the_forwarded_eml_is_one_part_and_its_children_are_not_listed(http, adapter):
    names = [p.filename for p in await adapter.list_parts(CREDS, MSG)]
    assert "forwarded.eml" in names
    assert "child_inside_eml.pdf" not in names


async def test_two_parts_with_one_name_stay_two_parts_with_distinct_ids_and_handles(http, adapter):
    same = [p for p in await adapter.list_parts(CREDS, MSG) if p.filename == PDF_NAME]
    assert len(same) == 2
    assert len({p.part_id for p in same}) == 2 and len({p.handle for p in same}) == 2


async def test_the_listed_names_are_the_names_the_index_stores(http, adapter):
    payload = _fixture("gmail_multipart_nested.json")["payload"]
    _, _, indexed = GmailProviderAdapter._parse_payload(payload)
    listed = [p.filename for p in await adapter.list_parts(CREDS, MSG)]
    assert listed == indexed


async def test_a_message_without_attachments_lists_nothing(monkeypatch, adapter):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages/m1", 200, _fixture("gmail_multipart_plain.json"))
    assert await adapter.list_parts(CREDS, "m1") == []


async def test_a_part_without_a_part_id_gets_its_positional_path(monkeypatch, adapter):
    doc = _fixture("gmail_multipart_nested.json")
    del doc["payload"]["parts"][1]["partId"]
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages/m1", 200, doc)
    parts = await adapter.list_parts(CREDS, "m1")
    assert [p.part_id for p in parts if p.filename == PDF_NAME] == ["1", "2"]


async def test_get_part_downloads_the_attachment_by_its_handle(http, adapter):
    pdf = b"%PDF-1.4 invented invoice body"
    http.route("GET", f"{BASE}/messages/{MSG}/attachments/ATT-PDF-a", 200,
               {"size": len(pdf), "data": _enc(pdf)})
    part = EmailPart("1", PDF_NAME, "application/pdf", 20000, "ATT-PDF-a")

    assert await adapter.get_part(CREDS, MSG, part, max_bytes=1_000_000) == pdf
    (request,) = http.requests
    assert request.url == f"{BASE}/messages/{MSG}/attachments/ATT-PDF-a"
    assert request.headers["Authorization"] == "Bearer TOKEN-1"


async def test_get_part_accepts_padded_and_unpadded_base64(http, adapter):
    for data in (b"a", b"ab", b"abc"):
        http.route("GET", f"{BASE}/messages/{MSG}/attachments/ATT-PDF-a", 200, {"data": _enc(data, pad=True)})
        part = EmailPart("1", PDF_NAME, "application/pdf", 1, "ATT-PDF-a")
        assert await adapter.get_part(CREDS, MSG, part, max_bytes=100) == data


async def test_an_inline_part_is_read_from_the_message_again(http, adapter):
    part = EmailPart("4", "totals.csv", "text/csv", 24, "")
    assert await adapter.get_part(CREDS, MSG, part, max_bytes=1000) == b"item,amount\nwidget,100\n"
    assert [r.url for r in http.requests] == [f"{BASE}/messages/{MSG}"]


async def test_an_inline_part_missing_from_the_message_is_empty(http, adapter):
    part = EmailPart("9", "gone.csv", "text/csv", 5, "")
    assert await adapter.get_part(CREDS, MSG, part, max_bytes=1000) == b""


async def test_a_part_with_no_data_is_empty(http, adapter):
    http.route("GET", f"{BASE}/messages/{MSG}/attachments/ATT-PDF-a", 200, {"size": 0})
    part = EmailPart("1", PDF_NAME, "application/pdf", 0, "ATT-PDF-a")
    assert await adapter.get_part(CREDS, MSG, part, max_bytes=100) == b""


async def test_a_declared_size_over_the_cap_is_refused_before_any_request(http, adapter):
    part = EmailPart("1", PDF_NAME, "application/pdf", 5000, "ATT-PDF-a")
    with pytest.raises(EmailPartTooLarge) as exc:
        await adapter.get_part(CREDS, MSG, part, max_bytes=4999)
    assert exc.value.limit_bytes == 4999
    assert http.requests == []


async def test_a_size_lie_is_caught_on_the_bytes_that_arrive(http, adapter):
    http.route("GET", f"{BASE}/messages/{MSG}/attachments/ATT-PDF-a", 200, {"size": 10, "data": _enc(b"x" * 5000)})
    part = EmailPart("1", PDF_NAME, "application/pdf", 10, "ATT-PDF-a")
    with pytest.raises(EmailPartTooLarge):
        await adapter.get_part(CREDS, MSG, part, max_bytes=4999)


async def test_bytes_exactly_at_the_cap_are_returned(http, adapter):
    http.route("GET", f"{BASE}/messages/{MSG}/attachments/ATT-PDF-a", 200, {"data": _enc(b"x" * 4999)})
    part = EmailPart("1", PDF_NAME, "application/pdf", 10, "ATT-PDF-a")
    assert await adapter.get_part(CREDS, MSG, part, max_bytes=4999) == b"x" * 4999


@pytest.mark.parametrize("status,error", [
    (404, EmailMessageGone), (429, EmailPartUnavailable), (500, EmailPartUnavailable), (503, EmailPartUnavailable),
])
async def test_list_parts_maps_the_status_to_a_typed_error(monkeypatch, adapter, status, error):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages/m1", status, {"error": {"code": status}})
    with pytest.raises(error):
        await adapter.list_parts(CREDS, "m1")


@pytest.mark.parametrize("status,error", [(404, EmailMessageGone), (429, EmailPartUnavailable)])
async def test_get_part_maps_the_status_to_a_typed_error(http, adapter, status, error):
    http.route("GET", f"{BASE}/messages/{MSG}/attachments/ATT-PDF-a", status, {"error": {"code": status}})
    with pytest.raises(error):
        await adapter.get_part(CREDS, MSG, EmailPart("1", PDF_NAME, "application/pdf", 10, "ATT-PDF-a"), 100)


async def test_a_timeout_and_a_dropped_connection_mean_the_mailbox_did_not_answer(monkeypatch, adapter):
    fake = http_boundary_fake.install(monkeypatch)

    def slow(_req):
        raise asyncio.TimeoutError()

    fake.route("GET", f"{BASE}/messages/m1", handler=slow)
    with pytest.raises(EmailPartUnavailable):
        await adapter.list_parts(CREDS, "m1")
    # an unrouted URL is aiohttp.ClientConnectionError in the fake: the same class of failure
    with pytest.raises(EmailPartUnavailable):
        await adapter.list_parts(CREDS, "m2")
    assert [r.url for r in fake.unrouted] == [f"{BASE}/messages/m2"]


async def test_an_insufficient_scope_403_keeps_its_own_error(monkeypatch, adapter):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages/m1", 403,
               {"error": {"code": 403, "status": "PERMISSION_DENIED",
                          "errors": [{"reason": "insufficientPermissions"}]}})
    with pytest.raises(OAuthScopeInsufficientError):
        await adapter.list_parts(CREDS, "m1")


async def test_another_status_is_not_swallowed(monkeypatch, adapter):
    import aiohttp
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages/m1", 401, {"error": {"code": 401}})
    with pytest.raises(aiohttp.ClientResponseError) as exc:
        await adapter.list_parts(CREDS, "m1")
    assert exc.value.status == 401
