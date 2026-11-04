"""render_html.js network layers that need no browser (slice S2, §2 Network): the request allowlist (`isAllowedUrl`) and the
Chromium flags (`launchArgs`). Loaded with node alone: the renderer requires puppeteer only inside main()."""
import json
import os
import shutil
import subprocess

import pytest

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_SCRIPT = os.path.join(_ROOT_DIR, "pdf_generator", "render_html.js")
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="needs node")

_ALLOWED = ["data:image/png;base64,iVBORw0KGgo=", "data:text/css,body{color:red}", "about:blank", "about:srcdoc",
            "blob:null/6c1f4a0e-abcd-4ef0-8abc-def0abcdef01", "DATA:text/plain,x"]
_BLOCKED = ["https://example.com/logo.png", "http://169.254.169.254/computeMetadata/v1/",
            "http://metadata.google.internal/", "http://127.0.0.1:1/", "http://[::1]/", "file:///etc/passwd",
            "ftp://example.com/", "ws://127.0.0.1:1/", "chrome://version", "javascript:alert(1)", "", " data:x"]
_NOT_STRINGS_JS = "[null, undefined, 42, {}, ['data:x']]"


def _node(expression: str):
    """Evaluates `expression` with the renderer module bound to `m`; returns its JSON value."""
    program = f"const m = require({json.dumps(_SCRIPT)}); process.stdout.write(JSON.stringify({expression}));"
    out = subprocess.run([_NODE, "-e", program], capture_output=True, timeout=30, check=True,
                         env={"PATH": os.environ.get("PATH", "")})
    return json.loads(out.stdout)


def test_allowlist():
    urls = _ALLOWED + _BLOCKED
    verdicts = _node(f"{json.dumps(urls)}.map(m.isAllowedUrl)")
    assert dict(zip(urls, verdicts)) == {**{u: True for u in _ALLOWED}, **{u: False for u in _BLOCKED}}


def test_a_non_string_is_never_allowed():
    assert _node(f"{_NOT_STRINGS_JS}.map(m.isAllowedUrl)") == [False] * 5


def test_launch_flags():
    without, own = _node("[m.launchArgs('0'), m.launchArgs('1')]")
    network = {"--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
               "--disable-background-networking", "--disable-component-update", "--no-pings",
               "--disable-crash-reporter", "--disable-breakpad", "--disable-dev-shm-usage"}
    assert network <= set(without) and network <= set(own)
    assert {"--no-sandbox", "--disable-setuid-sandbox"} <= set(without)
    assert not {"--no-sandbox", "--disable-setuid-sandbox"} & set(own)
    assert not any(flag.startswith("--remote-debugging-port") for flag in without + own)


def test_requiring_the_module_does_not_run_the_renderer():
    """No stdin read, no browser: loading the module for its exports is side-effect free."""
    assert _node("Object.keys(m).sort()") == ["isAllowedUrl", "launchArgs"]
