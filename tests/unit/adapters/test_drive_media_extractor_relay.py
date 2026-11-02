"""The extractor with the relay as its child-URL opener (GOOGLE_DRIVE_RFC G2, E24): for a header-carrying source the
child's request, argv and environment hold no header value and its url is the relay's loopback url; a header-free
`http://` source is refused; a header-free `https://` source reaches the child unchanged."""
import functools
import json
import ssl

import pytest
from aiohttp import web

from src.adapters.drive_parser import media_relay
from src.adapters.drive_parser.media_extractor import SubprocessMediaExtractor
from src.adapters.drive_parser.media_relay import LoopbackMediaRelay, relay_child_url
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaUnreadable, WindowSpec
from tests.unit.adapters.drive_media_fixtures import make_ca_and_server_cert

STUB = "tests.unit.adapters.drive_media_relay_child_stub"
TOKEN = "tok-a"
WEBM = "matroska,webm"
WALL_S = 60.0


class _Upstream:
    def __init__(self, directory) -> None:
        self.ca, cert, key = make_ca_and_server_cert(directory)
        self._cert, self._key = cert, key
        self.auth = []
        self.port = 0

    async def start(self) -> "_Upstream":
        async def serve(request):
            self.auth.append(request.headers.get("Authorization"))
            return web.Response(body=b"z" * 400, headers={"Content-Type": "video/mp4"})

        app = web.Application()
        app.router.add_get("/{tail:.*}", serve)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._cert, self._key)
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]    # type: ignore[union-attr]
        return self

    def trusting(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=self.ca)
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return context

    @property
    def url(self) -> str:
        return f"https://localhost:{self.port}/Hamburg%20talk.mp4"


@pytest.fixture
async def upstream(tmp_path, monkeypatch):
    server = await _Upstream(tmp_path).start()
    monkeypatch.setattr(media_relay, "LoopbackMediaRelay",
                        functools.partial(LoopbackMediaRelay, ssl_context=server.trusting()))
    yield server
    await server._runner.cleanup()


def _extractor(tmp_path) -> SubprocessMediaExtractor:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return SubprocessMediaExtractor(child_module=STUB, temp_root=str(root), ca_file="/x/ca.pem",
                                    child_url=relay_child_url)


async def _run(tmp_path, source: MediaSource) -> dict:
    out = tmp_path / "out"
    outcome = await _extractor(tmp_path).extract_windows(
        source, [WindowSpec(index=0, start_s=0.0, duration_s=10.0)], str(out), wall_s=WALL_S, container=WEBM)
    assert outcome.windows
    return json.loads((out / "window_0.opus").read_text())


async def test_a_header_carrying_source_reaches_the_child_only_as_the_relay_url(tmp_path, upstream):
    source = MediaSource(upstream.url, {"Authorization": f"Bearer {TOKEN}"})
    dump = await _run(tmp_path, source)
    assert dump["request"]["url"].startswith("http://127.0.0.1:")
    assert dump["fetched"] == {"status": 200, "length": 400}
    assert upstream.auth == [f"Bearer {TOKEN}"]
    for part in ("request", "argv", "env"):
        assert TOKEN not in json.dumps(dump[part])
    assert "Hamburg" not in json.dumps(dump)


async def test_a_header_free_http_source_is_refused(tmp_path, upstream):
    with pytest.raises(MediaUnreadable):
        await _run(tmp_path, MediaSource("http://media.example.invalid/Hamburg%20talk.mp4"))


async def test_a_header_free_https_source_reaches_the_child_unchanged(tmp_path, upstream):
    source = MediaSource("https://media.example.invalid/Hamburg%20talk.mp4?sig=abc")
    dump = await _run(tmp_path, source)
    assert dump["request"]["url"] == source.url
    assert dump["fetched"] is None
