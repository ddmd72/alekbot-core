"""Dockerfile.sandbox `render` target: chrome-headless-shell + fonts for render.pdf / render.png (S2 card C)."""
import re
import shutil
import subprocess

import pytest

from tests.unit.sandbox_closure import ROOT

_TEXT = (ROOT / "Dockerfile.sandbox").read_text()
_FONTS = (
    "fonts-noto-color-emoji", "fonts-unifont", "libfontconfig1", "libfreetype6", "xfonts-scalable", "fonts-liberation",
    "fonts-ipafont-gothic", "fonts-wqy-zenhei", "fonts-tlwg-loma-otf", "fonts-freefont-ttf",
)


def _render_instructions() -> str:
    joined = re.sub(r"\\\n", " ", _TEXT)
    render = re.split(r"^FROM .* AS light", joined, flags=re.M)[0]
    return " ".join(ln.strip() for ln in render.splitlines() if ln.strip() and not ln.strip().startswith("#"))


def test_installs_only_the_headless_shell_into_opt_puppeteer():
    ins = _render_instructions()
    assert "PUPPETEER_CACHE_DIR=/opt/puppeteer" in ins
    assert re.search(r"@puppeteer/browsers install chrome-headless-shell@\d+\.\d+\.\d+\.\d+ --path /opt/puppeteer", ins)
    assert "--install-deps" in ins
    assert not re.search(r"browsers install chrome@|install chromium|chrome-for-testing", ins)


def test_pinned_version_matches_what_puppeteer_expects():
    revisions = ROOT / "pdf_generator" / "node_modules" / "puppeteer-core" / "lib" / "cjs" / "puppeteer" / "revisions.js"
    if not revisions.is_file():
        pytest.skip("pdf_generator/node_modules not installed")
    pinned = re.search(r"chrome-headless-shell@(\S+)", _render_instructions()).group(1)
    assert f"'chrome-headless-shell': '{pinned}'" in revisions.read_text()


def test_fonts_are_installed_and_no_playwright():
    ins = _render_instructions()
    for font in _FONTS:
        assert font in ins, font
    assert "xvfb" not in ins.lower() and "playwright" not in ins.lower()


def test_puppeteer_dependencies_omit_dev():
    assert "npm install --omit=dev" in _render_instructions()
    assert "COPY pdf_generator/package.json" in _render_instructions()


def test_world_writable_gate_covers_opt_puppeteer():
    ins = _render_instructions()
    assert "chown -R root:root /app /opt/puppeteer" in ins
    assert "chmod -R go-w /app /opt/puppeteer" in ins
    assert re.search(r"find /app /opt/puppeteer \S+ .*-perm -o\+w", ins)


def test_both_targets_copy_the_html_engine_modules_and_png_frame():
    light_start = re.search(r"^FROM .* AS light", _TEXT, re.M).start()
    for part in (_TEXT[:light_start], _TEXT[light_start:]):
        for name in ("html_render_engine.py", "render_html_child.py", "sandbox_png_frame.py"):
            assert name in part, name


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_built_image_has_the_browser_closed_to_the_engine_user():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker daemon not running")
    tag = "sandbox-render-browser-test"
    subprocess.run(["docker", "build", "--target", "render", "-f", "Dockerfile.sandbox", "-t", tag, "."],
                   cwd=ROOT, check=True)
    script = ("f=$(find /opt/puppeteer -name chrome-headless-shell -type f | head -1); test -x \"$f\" && "
              "! su -s /bin/sh sandboxengine -c \"touch /opt/puppeteer/x\" 2>/dev/null && echo ok")
    out = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", script],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr
