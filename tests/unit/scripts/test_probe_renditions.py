"""Spike S0g script (scripts/onedrive/probe_renditions.py; USER_DRIVE_INDEXING_RFC §18, §24.5) run against a fake
Graph: the same `/thumbnails?select=<size>` call the adapter makes, renditions fetched without the bearer token and
saved only under the output folder, a per-size summary."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_DIR = Path(__file__).resolve().parents[3] / "scripts" / "onedrive"
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location("probe_renditions", _DIR / "probe_renditions.py")
probe_renditions = importlib.util.module_from_spec(_spec)
with patch("dotenv.load_dotenv"):   # the probe scripts load .env on import; the unit suite never reads it
    _spec.loader.exec_module(probe_renditions)


class _Graph:
    def __init__(self):
        self.calls = []

    async def call(self, method, url, **kw):
        self.calls.append(url)
        if url.startswith("/me/drive/special/approot:/"):
            return 200, {"id": "folder"}, {}
        if url.endswith("/children?$top=200"):
            return 200, {"value": [{"id": "b", "name": "receipt.heic", "file": {"mimeType": "image/heic"}},
                                   {"id": "x", "name": "sub", "folder": {}},
                                   {"id": "a", "name": "A4.png", "file": {"mimeType": "image/png"}}]}, {}
        item, size = url.split("/items/")[1].split("/thumbnails?select=")
        if size == "c4096x4096":
            return 400, {"error": {"code": "invalidRequest"}}, {}
        return 200, {"value": [{"id": "0", size: {"width": 800, "height": 600,
                                                 "url": f"https://thumbs.example/{item}/{size}"}}]}, {}


def _plain():
    session = MagicMock()

    def get(url):
        resp = MagicMock(status=200, headers={"Content-Type": "image/jpeg"})
        resp.read = AsyncMock(return_value=b"\xff\xd8\xff" + url.encode())
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    session.get.side_effect = get
    return session


async def test_saves_each_rendition_and_reports_per_size(tmp_path):
    graph, plain = _Graph(), _plain()
    report = await probe_renditions.probe(graph, plain, "S0g", tmp_path)
    assert [i["name"] for i in report["items"]] == ["A4.png", "receipt.heic"]
    assert "/me/drive/items/a/thumbnails?select=c2048x2048" in graph.calls
    saved = sorted(p.name for p in tmp_path.iterdir())
    assert "01_c2048x2048.jpg" in saved and "02_medium.jpg" in saved and not any("c4096" in n for n in saved)
    assert report["items"][1]["sizes"]["c4096x4096"] == {"status": 400, "saved": False,
                                                          "body": {"error": {"code": "invalidRequest"}}}
    assert report["summary"]["c2048x2048"] == {"image/png": 1, "image/heic": 1}
    assert report["summary"]["c4096x4096"] == {}
    assert all("headers" not in c.kwargs for c in plain.get.call_args_list)


def test_rendition_url_needs_https_and_the_size_key():
    page = {"value": [{"medium": {"url": "https://t/1"}}]}
    assert probe_renditions.rendition_url(page, "medium") == "https://t/1"
    assert probe_renditions.rendition_url(page, "large") is None
    assert probe_renditions.rendition_url({"value": [{"medium": {"url": "http://t/1"}}]}, "medium") is None
    assert probe_renditions.rendition_url({"value": []}, "medium") is None
