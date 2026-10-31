"""The Cabinet drive card keeps its long privacy paragraphs collapsed behind a <details>; live status stays visible."""
import re
from pathlib import Path

import pytest

_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"
LANGS = ("en", "uk", "fr", "es")
DETAILS = re.compile(r'<details class="disclosure">(.*?)</details>', re.S)


def _html():
    return _HTML.read_text(encoding="utf-8")


def _dicts():
    body = re.search(r"const LANG_DICT = \{(.*?)\n      \};", _html(), re.S).group(1)
    out = {}
    for lang in LANGS:
        block = re.search(rf"\n        {lang}: \{{(.*?)\n        \}},", body, re.S).group(1)
        out[lang] = dict(re.findall(r'^\s*"([\w.]+)": "((?:[^"\\]|\\.)*)",?$', block, re.M))
    return out


def _function_source(name):
    html = _html()
    start = html.index(f"function {name}(")
    nxt = re.search(r"\n      (?:async )?function \w+\(", html[start + 1:])
    return html[start:start + 1 + nxt.start()] if nxt else html[start:]


@pytest.mark.parametrize("lang", LANGS)
def test_disclosure_summary_exists_in_every_language(lang):
    assert _dicts()[lang].get("drive.disclosure_summary", "").strip()


@pytest.mark.parametrize("fn,key", [
    ("renderDriveIndexing", "drive.index_disclosure"),
    ("renderDeepResearchCopy", "drive.dr_copy_disclosure"),
])
def test_disclosure_is_rendered_inside_collapsed_details(fn, key):
    blocks = DETAILS.findall(_function_source(fn))
    inside = [b for b in blocks if f't("{key}")' in b]
    assert len(inside) == 1
    assert 'open' not in re.search(r"<details[^>]*>", _function_source(fn)).group(0)
    assert re.search(r'<summary>\s*\$\{t\("drive\.disclosure_summary"\)\}\s*</summary>', inside[0])


def test_live_status_block_stays_outside_the_details():
    src = _function_source("renderDriveIndexing")
    closing = src.index("</details>")
    assert "${details}" in src[closing:]
    assert all("${details}" not in b for b in DETAILS.findall(src))
