"""
render.pdf / render.png engine child (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6, §4.7; slice S2): runs under `limits_exec`, as the
engine uid.

    python -B -I <path>/render_html_child.py <body-file> <workdir> <pdf|png> <browser-cache-dir> <sandbox:0|1>

Body: `render.pdf` = raw UTF-8 HTML; `render.png` = `u32-be width | html` (`sandbox_png_frame`). The server writes the
body to a file and never splits it; this child does, refuses an empty page or a bad frame (EXIT_BAD_REQUEST), writes
`page.html` into the workdir, then replaces itself with the fixed `node pdf_generator/render_html.js <mode> <width>
<sandbox>`, so the limits inherited from `limits_exec` carry over: stdin = page.html, stdout = out.bin (both in the
workdir); stderr stays the launcher's temp file, which nobody ever reads back as text. The width of a pdf call is 0.

Node's environment is fixed here, not inherited: PATH and LANG from the launcher's allowlisted environment, HOME and
TMPDIR = the workdir (Chromium's profile and shm files land on the size-limited volume), NODE_PATH = the renderer's own
modules, PUPPETEER_CACHE_DIR = the browser cache argument.

Started by file path, not `-m`: `limits_exec` has already changed into the workdir, where `src` cannot be found, so the
module appends the repository root to sys.path itself (last, under `-I`) before importing the frame codec.

Exit codes: EXIT_BAD_REQUEST = unreadable body, bad frame, empty HTML or bad arguments; EXIT_EXEC_FAILED = `node` could
not be started. Anything else is node's own exit status (render_html.js: 0 ok, 1 render failed, 2 no browser).
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Sequence, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)   # last: nothing in the repo root can shadow a stdlib module imported after this

from src.domain.sandbox_png_frame import BadPngBody, split_png_body  # noqa: E402

EXIT_BAD_REQUEST = 4    # same meaning as limits_exec / render_docx_child
EXIT_EXEC_FAILED = 5

MODE_PDF = "pdf"
MODE_PNG = "png"
MODES = (MODE_PDF, MODE_PNG)
SANDBOX_FLAGS = ("0", "1")

PAGE_FILE = "page.html"
OUTPUT_FILE = "out.bin"

RENDER_SCRIPT = os.path.join(_REPO_ROOT, "pdf_generator", "render_html.js")
NODE_MODULES = os.path.join(_REPO_ROOT, "pdf_generator", "node_modules")
_INHERITED_ENV = ("PATH", "LANG")


class BadPage(ValueError):
    """The body holds no usable page (empty HTML)."""


def read_page(body_path: str, mode: str) -> Tuple[int, bytes]:
    """(width, html) from the body file; width is 0 for pdf. Raises OSError, BadPngBody or BadPage."""
    with open(body_path, "rb") as handle:
        body = handle.read()
    width, html = split_png_body(body) if mode == MODE_PNG else (0, body)
    if not html.strip():
        raise BadPage("empty HTML")
    return width, html


def node_argv(mode: str, width: int, sandbox: str) -> List[str]:
    return ["node", RENDER_SCRIPT, mode, str(width), sandbox]


def node_env(workdir: str, browser_cache_dir: str) -> Dict[str, str]:
    env = {key: value for key in _INHERITED_ENV if (value := os.environ.get(key))}
    env.update({"HOME": workdir, "TMPDIR": workdir, "NODE_PATH": NODE_MODULES,
                "PUPPETEER_CACHE_DIR": browser_cache_dir})
    return env


def _create(path: str) -> int:
    """A new file only: an existing entry (or link) at `path` is an error."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def write_page(workdir: str, html: bytes) -> None:
    with os.fdopen(_create(os.path.join(workdir, PAGE_FILE)), "wb") as handle:
        handle.write(html)


def _valid_args(argv: Sequence[str]) -> bool:
    return len(argv) == 5 and argv[2] in MODES and bool(argv[3]) and argv[4] in SANDBOX_FLAGS


def main(argv: Sequence[str]) -> int:
    if not _valid_args(argv):
        sys.stderr.write("render_html_child: expected <body-file> <workdir> <pdf|png> <browser-cache-dir> <0|1>\n")
        return EXIT_BAD_REQUEST
    body_path, workdir, mode, browser_cache_dir, sandbox = argv
    try:
        width, html = read_page(body_path, mode)
        write_page(workdir, html)
    except (OSError, BadPngBody, BadPage) as exc:
        sys.stderr.write(f"render_html_child: {type(exc).__name__}\n")
        return EXIT_BAD_REQUEST
    try:
        stdin_fd = os.open(os.path.join(workdir, PAGE_FILE), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stdout_fd = _create(os.path.join(workdir, OUTPUT_FILE))
        os.dup2(stdin_fd, 0)
        os.dup2(stdout_fd, 1)
        os.execvpe("node", node_argv(mode, width, sandbox), node_env(workdir, browser_cache_dir))
    except OSError as exc:
        sys.stderr.write(f"render_html_child: node could not be started: {type(exc).__name__}\n")
        return EXIT_EXEC_FAILED
    return EXIT_EXEC_FAILED   # unreachable: execvpe replaces the process


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
