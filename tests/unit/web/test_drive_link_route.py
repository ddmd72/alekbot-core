"""`GET|HEAD /d/<id>` over the production `DriveLinkService` of the slice-4 world (DRIVE_MCP_CONNECTOR_RFC §8):
headers, streaming with no Content-Length and no response timeout, no redirect ever, HEAD free, one 404 for every
refusal."""
import asyncio
from typing import Any, Dict, List, Tuple

import pytest
from quart import Quart

from src.domain.drive_audience import connector_audience
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_link import LINK_TTL_S
from src.domain.drive_media_source import MediaSource
from src.web.drive_link_app import NOT_VALID, create_drive_link_blueprint
from tests.unit.services.drive_link_world import ACCOUNT, GRANT, INVOICE_BYTES, USER, LinkWorld

SAFETY = {"cache-control": "no-store", "referrer-policy": "no-referrer", "x-content-type-options": "nosniff",
          "content-security-policy": "sandbox; default-src 'none'"}


class Route:
    def __init__(self, world: LinkWorld) -> None:
        self.world = world
        self.app = Quart(__name__)
        self.app.register_blueprint(create_drive_link_blueprint(world.service()))
        self.responses: List[Any] = []

        @self.app.after_request
        async def keep(response):
            self.responses.append(response)
            return response

    async def issue(self, item: str) -> str:
        with connector_audience():
            issued = await self.world.service().issue(USER, ACCOUNT, GRANT, self.world.ref(item))
        return issued.url.rsplit("/", 1)[1]

    async def asgi(self, method: str, raw: str) -> Tuple[int, Dict[str, str], bytes]:
        """One request at the ASGI level: the headers exactly as sent (the test client adds a Content-Length)."""
        sent: List[dict] = []
        path = f"/d/{raw}"
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": method, "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "root_path": "", "headers": [(b"host", b"mcp.example.test")], "client": ("127.0.0.1", 1),
                 "server": ("mcp.example.test", 443), "extensions": {}}
        messages = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive() -> dict:
            if messages:
                return messages.pop()
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        await self.app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], headers, body


@pytest.fixture
async def route():
    world = await LinkWorld().open()
    r = Route(world)
    await r.app.startup()
    yield r
    await r.app.shutdown()


async def test_get_streams_the_bytes_with_the_headers_and_no_content_length(route):
    raw = await route.issue(route.world.invoice)
    status, headers, body = await route.asgi("GET", raw)
    assert status == 200 and body == INVOICE_BYTES
    assert "content-length" not in headers
    assert headers["content-type"] == "application/pdf"
    assert headers["content-disposition"].startswith('inline; filename="FA-0137.pdf"')
    assert {k: headers[k] for k in SAFETY} == SAFETY


async def test_get_never_redirects(route):
    for item in (route.world.invoice, route.world.plan, route.world.image):
        raw = await route.issue(item)
        status, headers, _ = await route.asgi("GET", raw)
        assert status == 200 and "location" not in headers
    header_free = MediaSource(url="https://signed.example.invalid/fa-0137")
    assert header_free.header_free
    raw = await route.issue(route.world.invoice)
    route.world.media.bodies[header_free.url] = b"signed bytes"
    original = route.world.adapter.media_source

    async def presigned(handle, item_id):
        return header_free

    route.world.adapter.media_source = presigned          # type: ignore[method-assign]
    try:
        status, headers, body = await route.asgi("GET", raw)
    finally:
        route.world.adapter.media_source = original        # type: ignore[method-assign]
    assert (status, body) == (200, b"signed bytes") and "location" not in headers


async def test_the_response_has_no_timeout(route):
    raw = await route.issue(route.world.plan)
    client = route.app.test_client()
    response = await client.get(f"/d/{raw}")
    assert response.status_code == 200
    assert route.responses[-1].timeout is None
    await client.head(f"/d/{await route.issue(route.world.plan)}")
    assert route.responses[-1].timeout is None


async def test_head_answers_the_headers_with_no_body_and_consumes_nothing(route):
    raw = await route.issue(route.world.plan)
    status, headers, body = await route.asgi("HEAD", raw)
    assert (status, body) == (200, b"")
    assert headers["content-disposition"].startswith('attachment; filename="Tom\'s plan.docx"')
    assert {k: headers[k] for k in SAFETY} == SAFETY
    assert route.world.media.calls == []
    assert next(iter(route.world.store.links.values())).reads == 0
    assert (await route.asgi("GET", raw))[0] == 200


async def test_unknown_spent_expired_and_refused_links_answer_the_same_404(route):
    w = route.world
    answers = []
    answers.append(await route.asgi("GET", "B" * 22))                        # unknown
    spent = await route.issue(w.plan)
    await route.asgi("GET", spent)
    answers.append(await route.asgi("GET", spent))                           # spent
    expired = await route.issue(w.plan)
    w.later(LINK_TTL_S)
    answers.append(await route.asgi("GET", expired))                         # expired
    w.now = w.now.replace(hour=10)
    refused = await route.issue(w.invoice)
    await w.share(ConnectorAccess.folders((w.rnd,)))
    answers.append(await route.asgi("GET", refused))                         # its folder left access
    answers.append(await route.asgi("HEAD", refused))
    for status, headers, body in answers:
        assert status == 404
        assert body in (NOT_VALID.encode(), b"")
        assert headers["content-type"].startswith("text/plain")
        assert {k: headers[k] for k in ("cache-control", "referrer-policy", "x-content-type-options")} == {
            k: SAFETY[k] for k in ("cache-control", "referrer-policy", "x-content-type-options")}
    assert {a[2] for a in answers[:4]} == {NOT_VALID.encode()}


@pytest.mark.parametrize("raw", ["short", "A" * 23, "A" * 21 + "!", "A" * 21 + "."])
async def test_a_malformed_id_reads_no_store(route, raw):
    status, _, body = await route.asgi("GET", raw)
    assert (status, body) == (404, NOT_VALID.encode())
    assert route.world.store.reads == 0


async def test_an_html_file_is_never_served_inline(route):
    w = route.world
    page = w.adapter.plant(w.handle, w.oak, "notice.html", b"<script>alert(1)</script>", mime="text/html")
    w.media.bodies[(await w.adapter.media_source(w.handle, page)).url] = b"<script>alert(1)</script>"
    raw = await route.issue(page)
    status, headers, _ = await route.asgi("GET", raw)
    assert status == 200
    assert headers["content-type"] == "application/octet-stream"
    assert headers["content-disposition"].startswith("attachment; ")
    assert headers["content-security-policy"] == SAFETY["content-security-policy"]
