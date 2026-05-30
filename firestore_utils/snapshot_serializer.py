"""Pure, round-trippable serialization for the prompt-token git mirror.

No I/O, no Firestore, no src/ imports — only PyYAML. Three document shapes:
  token     -> YAML frontmatter (metadata) + body (content), ".groovy"
  blueprint -> plain YAML
  profile   -> plain YAML

See docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md.
"""
from __future__ import annotations

import yaml

# Server-managed fields excluded from the snapshot — they change on every Firestore
# write and would add diff noise even when prompt content is identical.
_VOLATILE_KEYS = {"created_at", "updated_at"}

_DELIM = "---"


def _strip_volatile(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in _VOLATILE_KEYS}


def token_to_file(doc: dict) -> str:
    """token doc -> 'frontmatter + body' text. `content` becomes the body; rest is frontmatter."""
    front = _strip_volatile(doc)
    body = front.pop("content", "")
    front_yaml = yaml.safe_dump(
        front, sort_keys=True, allow_unicode=True, default_flow_style=False
    ).strip()
    return f"{_DELIM}\n{front_yaml}\n{_DELIM}\n{body}"


def token_from_file(text: str) -> dict:
    """Inverse of token_to_file. Reconstructs the doc dict (minus volatile keys)."""
    if not text.startswith(f"{_DELIM}\n"):
        raise ValueError("token file missing leading frontmatter delimiter")
    after = text[len(f"{_DELIM}\n"):]
    front_yaml, sep, body = after.partition(f"\n{_DELIM}\n")
    if not sep:
        raise ValueError("token file missing closing frontmatter delimiter")
    doc = yaml.safe_load(front_yaml) or {}
    doc["content"] = body
    return doc


def doc_to_yaml(doc: dict) -> str:
    """blueprint/profile doc -> plain YAML text."""
    return yaml.safe_dump(
        _strip_volatile(doc), sort_keys=True, allow_unicode=True, default_flow_style=False
    )


def doc_from_yaml(text: str) -> dict:
    """Inverse of doc_to_yaml."""
    return yaml.safe_load(text) or {}
