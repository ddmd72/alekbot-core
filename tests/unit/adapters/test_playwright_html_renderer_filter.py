"""Widget renderer: JavaScript off and no outbound requests (model-written HTML is untrusted)."""
import os
import socket
import threading

import pytest

from src.adapters.playwright_html_renderer import PlaywrightHtmlRenderer, is_request_allowed


@pytest.mark.parametrize(
    "url",
    ["data:image/png;base64,iVBORw0KGgo=", "about:blank", "blob:null/1234-abcd"],
)
def test_offline_urls_allowed(url):
    assert is_request_allowed(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/logo.png",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://metadata.google.internal/",
        "http://127.0.0.1:1/",
        "file:///etc/passwd",
    ],
)
def test_network_urls_refused(url):
    assert not is_request_allowed(url)


async def _chromium_available() -> bool:
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            return os.path.exists(pw.chromium.executable_path)
    except Exception:
        return False


class _Listener:
    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            conn.close()

    def close(self) -> None:
        self._stop = True
        self.sock.close()


@pytest.mark.slow
async def test_no_js_and_no_connections():
    if not await _chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    listener = _Listener()
    base = f"http://127.0.0.1:{listener.port}"
    html = f"""
    <html><head>
    <link rel="stylesheet" href="{base}/a.css">
    <style>.c{{background:url({base}/bg.png);padding:12px}}</style>
    </head><body>
    <div class="c" id="card">
      <h1 id="t">Albergo Immaginario</h1>
      <p>Booking RM-55120</p><p>10–14 Aug 2025</p><p>560 EUR</p>
      <img src="{base}/i.png"><iframe src="{base}/f.html"></iframe>
    </div>
    <script>
      fetch("{base}/x");
      var d = document.createElement("div"); d.style.height = "3000px";
      document.body.appendChild(d);
    </script></body></html>"""
    renderer = PlaywrightHtmlRenderer()
    try:
        png = await renderer.render(html, width=480)
        assert png.startswith(b"\x89PNG")
        # Scripts did not run: the injected 3000px div is absent (PNG height, 2x scale, from IHDR).
        height = int.from_bytes(png[20:24], "big")
        assert height < 1500
    finally:
        await renderer.stop()
        listener.close()
    assert listener.connections == 0
