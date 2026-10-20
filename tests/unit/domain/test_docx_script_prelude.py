"""The shared DOCX script prelude (plan G3): same blocked set as before the move, prepended before user code."""
import re

from src.adapters.node_docx_runner import _SAFE_ENV_KEYS, _SECURITY_PRELUDE
from src.domain.docx_script_prelude import SAFE_ENV_KEYS, SECURITY_PRELUDE

# Frozen copy of the set the in-process runner blocked before the prelude moved to domain/.
_BLOCKED_BEFORE_THE_MOVE = frozenset({
    "child_process", "cluster", "worker_threads", "inspector", "repl", "v8",
    "http", "http2", "https", "net", "tls", "dns", "dgram",
    "fs", "fs/promises",
})


def test_prelude_blocks_the_same_modules_as_before():
    block = re.search(r"new Set\(\[(.*?)\]\)", SECURITY_PRELUDE, re.S).group(1)
    assert frozenset(re.findall(r"'([^']+)'", block)) == _BLOCKED_BEFORE_THE_MOVE
    assert "globalThis.fetch = blockedFetch" in SECURITY_PRELUDE


def test_adapter_keeps_the_old_names():
    assert _SECURITY_PRELUDE is SECURITY_PRELUDE
    assert _SAFE_ENV_KEYS is SAFE_ENV_KEYS


def test_prelude_is_prepended_before_user_code(tmp_path):
    from src.adapters.sandbox_engine.render_docx_child import build_script

    script = build_script(b"console.log('user code');")
    assert script.startswith(SECURITY_PRELUDE.encode())
    assert script.index(b"Blocked module for security") < script.index(b"user code")
