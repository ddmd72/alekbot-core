"""The whole Cabinet source as one text, for the tests that read the page as text.

The Cabinet is a shell (`src/web/static/cabinet.html`) plus the assets it loads from `/cabinet/assets/<name>`
(CABINET_REDESIGN_RFC §6). A text test reads what the browser assembles: the shell with each asset's content put in
place of the tag that loads it (a stylesheet link becomes a `<style>` block, a script tag a `<script>` block), so a
check finds a function or a rule wherever the split put it, in load order. An asset the shell names and the folder
lacks fails here, loudly.
"""
import re
from functools import lru_cache
from pathlib import Path

STATIC = Path(__file__).resolve().parents[3] / "src" / "web" / "static"
ASSET_PREFIX = "/cabinet/assets/"

_STYLESHEET = re.compile(r'<link rel="stylesheet" href="' + ASSET_PREFIX + r'([A-Za-z0-9._-]+)"\s*/?>')
_SCRIPT = re.compile(r'<script src="' + ASSET_PREFIX + r'([A-Za-z0-9._-]+)"></script>')


@lru_cache(maxsize=1)
def cabinet_source() -> str:
    shell = (STATIC / "cabinet.html").read_text(encoding="utf-8")
    shell = _STYLESHEET.sub(lambda m: "<style>\n" + _asset(m.group(1)) + "\n</style>", shell)
    return _SCRIPT.sub(lambda m: "<script>\n" + _asset(m.group(1)) + "\n</script>", shell)


def _asset(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")
