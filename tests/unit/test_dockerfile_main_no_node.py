"""Dockerfile (main image): DOCX and PDF render in the sandbox, so the main image carries no Node, npm or ffmpeg and no
npm layer; Playwright Chromium stays for the widget renderer (UNTRUSTED_CONTENT_SANDBOX_RFC §4.12, owner decision O16)."""
import re
from pathlib import Path

_TEXT = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
_INSTRUCTIONS = [ln.strip() for ln in re.sub(r"\\\n", " ", _TEXT).splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]


def test_the_main_image_installs_no_node_npm_or_ffmpeg():
    for ins in _INSTRUCTIONS:
        assert not re.search(r"\b(nodejs|npm|ffmpeg)\b", ins), ins


def test_the_main_image_has_no_npm_layer_and_copies_no_node_package_manifest():
    assert not any("npm" in i or "package.json" in i for i in _INSTRUCTIONS)


def test_the_main_image_still_installs_playwright_chromium():
    assert any("playwright install chromium" in i for i in _INSTRUCTIONS)
