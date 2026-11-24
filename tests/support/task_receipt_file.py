"""The external task receipt file read back the way any reader would (owner's decision 2026-11-24): YAML front matter
(title, worker, note), a `---` line, then the brief byte for byte.

`receipt_parts(content)` splits the file's bytes at the closing `---` line and parses the front matter with
`yaml.safe_load`; the body comes back as bytes so a caller compares it to the brief byte for byte.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

import yaml


def receipt_parts(content: bytes) -> Tuple[Dict[str, Any], bytes]:
    """The parsed front matter and the bytes after its closing `---` line."""
    assert content.startswith(b"---\n"), content[:40]
    head, sep, body = content[len(b"---\n"):].partition(b"\n---\n")
    assert sep, "no closing --- line"
    return yaml.safe_load(head.decode("utf-8")), body
