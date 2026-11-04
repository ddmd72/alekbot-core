"""render_html_child (slice S2): body validation, page.html, node argv and environment (exec patched)."""
import os

import pytest

from src.adapters.sandbox_engine import render_html_child as child
from src.adapters.sandbox_engine.render_html_child import EXIT_BAD_REQUEST, EXIT_EXEC_FAILED
from src.domain.sandbox_png_frame import frame_png_body

_CACHE_DIR = "/opt/puppeteer"
_ROME_CARD = ("<div style=\"font-family:sans-serif;padding:16px;border-radius:12px;background:#f4efe6\">"
              "<h3>Albergo Immaginario</h3><p>Booking RM-55120</p><p>10–14 Aug 2025</p><p><b>560 EUR</b></p></div>")
_BUDGET = ("<!doctype html><html><head><style>@page{size:A4}</style></head><body><h1>Household budget 2026</h1>"
           "<table><tr><td>Rent</td><td>11,400</td></tr><tr><td>Groceries</td><td>4,200</td></tr>"
           "<tr><td>Transport</td><td>960</td></tr><tr><td>Holidays</td><td>2,500</td></tr></table></body></html>")


class _Exec(Exception):
    """Raised by the patched execvpe so the test process is never replaced."""


@pytest.fixture
def exec_calls(monkeypatch):
    """Patches execvpe and dup2 (the test's own stdin/stdout stay as they are); records the exec call."""
    calls = []

    def fake_exec(file, argv, env):
        calls.append((file, list(argv), dict(env)))
        raise _Exec()

    monkeypatch.setattr(child.os, "execvpe", fake_exec)
    monkeypatch.setattr(child.os, "dup2", lambda fd, target: None)
    return calls


def _body(tmp_path, data: bytes):
    path = tmp_path / "body"
    path.write_bytes(data)
    return str(path)


def _workdir(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return str(work)


@pytest.mark.parametrize("mode, data", [
    ("pdf", b""),
    ("pdf", b"  \n\t "),
    ("png", b""),                                   # no width header
    ("png", b"\x00\x00"),                           # short header
    ("png", (15).to_bytes(4, "big") + b"<p>x</p>"),     # width below the frame's range
    ("png", (2049).to_bytes(4, "big") + b"<p>x</p>"),   # width above it
    ("png", frame_png_body(480, b"")),              # a valid frame around an empty page
    ("png", frame_png_body(480, b" \n ")),
])
def test_bad_frame_or_empty_html_exits_bad_request(tmp_path, exec_calls, mode, data):
    workdir = _workdir(tmp_path)
    assert child.main([_body(tmp_path, data), workdir, mode, _CACHE_DIR, "0"]) == EXIT_BAD_REQUEST
    assert exec_calls == []
    assert not os.path.exists(os.path.join(workdir, "page.html"))


@pytest.mark.parametrize("argv", [
    [],
    ["body", "work", "pdf", _CACHE_DIR],                  # too few
    ["body", "work", "pdf", _CACHE_DIR, "0", "extra"],    # too many
    ["body", "work", "docx", _CACHE_DIR, "0"],            # unknown mode
    ["body", "work", "pdf", "", "0"],                     # no browser cache dir
    ["body", "work", "pdf", _CACHE_DIR, "yes"],           # sandbox flag not 0|1
])
def test_bad_arguments_exit_bad_request(exec_calls, argv):
    assert child.main(argv) == EXIT_BAD_REQUEST
    assert exec_calls == []


def test_missing_body_exits_bad_request(tmp_path, exec_calls):
    assert child.main([str(tmp_path / "missing"), _workdir(tmp_path), "pdf", _CACHE_DIR, "0"]) == EXIT_BAD_REQUEST


def test_pdf_writes_the_body_as_page_and_execs_the_renderer(tmp_path, exec_calls):
    workdir = _workdir(tmp_path)
    with pytest.raises(_Exec):
        child.main([_body(tmp_path, _BUDGET.encode()), workdir, "pdf", _CACHE_DIR, "0"])
    assert open(os.path.join(workdir, "page.html"), "rb").read() == _BUDGET.encode()
    assert os.path.isfile(os.path.join(workdir, "out.bin"))
    file, argv, _ = exec_calls[0]
    assert file == "node"
    assert argv == ["node", child.RENDER_SCRIPT, "pdf", "0", "0"]
    assert child.RENDER_SCRIPT.endswith(os.path.join("pdf_generator", "render_html.js"))


@pytest.mark.parametrize("width, sandbox", [(480, "0"), (320, "1")])
def test_png_splits_the_frame_and_passes_the_width(tmp_path, exec_calls, width, sandbox):
    workdir = _workdir(tmp_path)
    html = _ROME_CARD.encode()
    with pytest.raises(_Exec):
        child.main([_body(tmp_path, frame_png_body(width, html)), workdir, "png", _CACHE_DIR, sandbox])
    assert open(os.path.join(workdir, "page.html"), "rb").read() == html
    assert exec_calls[0][1] == ["node", child.RENDER_SCRIPT, "png", str(width), sandbox]


def test_node_environment_is_fixed(tmp_path, exec_calls, monkeypatch):
    monkeypatch.setenv("SANDBOX_TEST_SECRET_TOKEN", "s3cret-value")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    workdir = _workdir(tmp_path)
    with pytest.raises(_Exec):
        child.main([_body(tmp_path, _BUDGET.encode()), workdir, "pdf", _CACHE_DIR, "0"])
    env = exec_calls[0][2]
    assert env == {"PATH": "/usr/local/bin:/usr/bin", "LANG": "C.UTF-8", "HOME": workdir, "TMPDIR": workdir,
                   "NODE_PATH": child.NODE_MODULES, "PUPPETEER_CACHE_DIR": _CACHE_DIR}
    assert child.NODE_MODULES.endswith(os.path.join("pdf_generator", "node_modules"))


def test_existing_page_file_is_refused(tmp_path, exec_calls):
    """page.html is created fresh: a pre-placed file or link in the workdir is not written through."""
    workdir = _workdir(tmp_path)
    os.symlink(str(tmp_path / "elsewhere"), os.path.join(workdir, "page.html"))
    assert child.main([_body(tmp_path, _BUDGET.encode()), workdir, "pdf", _CACHE_DIR, "0"]) == EXIT_BAD_REQUEST
    assert not (tmp_path / "elsewhere").exists()


def test_node_that_cannot_start_exits_exec_failed(tmp_path, monkeypatch):
    def no_node(file, argv, env):
        raise FileNotFoundError(file)

    monkeypatch.setattr(child.os, "execvpe", no_node)
    monkeypatch.setattr(child.os, "dup2", lambda fd, target: None)
    assert child.main([_body(tmp_path, _BUDGET.encode()), _workdir(tmp_path), "pdf", _CACHE_DIR, "0"]) \
        == EXIT_EXEC_FAILED
