"""`geo.html` (GEO_MARKS_RFC §4.5): what the page may and may not do, read from the file the route serves. The owner
verified on a phone that Slack's in-app browser reports a denied permission while it returns the position, so the page
asks for the position at once and never reads the Permissions API."""
import re
from pathlib import Path

import pytest

from src.web.geo_mark_app import script_csp

HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "geo.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.DOTALL).group(1)  # type: ignore[union-attr]


def test_the_page_never_touches_the_permissions_api():
    assert "navigator.permissions" not in HTML
    assert "permissions" not in SCRIPT.lower()


def test_the_page_asks_for_a_fresh_high_accuracy_position_on_load():
    assert "navigator.geolocation.getCurrentPosition" in SCRIPT
    assert "enableHighAccuracy: true" in SCRIPT
    assert "maximumAge: 0" in SCRIPT
    assert re.search(r"timeout: 15000", SCRIPT)
    assert SCRIPT.rstrip().endswith("locate();\n})();")


def test_the_page_loads_nothing_from_outside():
    assert not re.search(r"(src|href|action)\s*=\s*[\"']?(https?:)?//", HTML)
    assert "http://" not in HTML and "https://" not in HTML
    assert "@import" not in HTML and "url(" not in HTML


def test_the_page_stores_nothing_in_the_browser():
    assert not re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie", HTML)


def test_the_post_fetch_sends_its_origin_under_the_no_referrer_policy():
    # `no-referrer` makes a same-origin non-GET fetch send `Origin: null` (Fetch spec); the route refuses that.
    assert 'referrerPolicy: "same-origin"' in SCRIPT[SCRIPT.index("fetch("):SCRIPT.index(".then(")]


def test_the_retry_asks_again_without_a_reload():
    assert 'retry.addEventListener("click", locate)' in SCRIPT


def test_the_csp_allows_exactly_the_inline_script_by_its_hash():
    csp = script_csp(HTML)
    assert re.search(r"script-src 'sha256-[A-Za-z0-9+/]{43}='", csp)
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert "unsafe-eval" not in csp
    # a changed script changes the hash
    assert script_csp(HTML.replace("maximumAge: 0", "maximumAge: 1")) != csp


def test_a_page_without_an_inline_script_has_no_csp():
    with pytest.raises(ValueError):
        script_csp("<html></html>")
