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

# Presence of any of these fields marks a document as account/user-scoped (PII).
# Such documents are never written to the git mirror (SECRETS RULE).
_PII_KEYS = {"user_id", "account_id"}


def is_pii_doc(doc_id: str, doc: dict) -> bool:
    """True if the document is account/user-scoped and must not be mirrored."""
    if _PII_KEYS & set(doc.keys()):
        return True
    # Mirrored collections hold named definitions ("COGNITIVE_PROCESS_SMART").
    # A uuid / 32-hex doc id signals a user-keyed document that slipped in.
    compact = doc_id.replace("-", "").lower()
    if len(compact) >= 32 and all(c in "0123456789abcdef" for c in compact):
        return True
    return False


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


# Mirrored collection kinds, in stable order.
_KINDS = ("tokens_system", "tokens_user", "blueprints", "profiles")


def relpath_for(kind: str, doc_id: str) -> str:
    """Relative path under prompts_snapshot/ for a given collection kind + doc id."""
    return {
        "tokens_system": f"tokens/system/{doc_id}.groovy",
        "tokens_user": f"tokens/user/{doc_id}.groovy",
        "blueprints": f"blueprints/{doc_id}.yaml",
        "profiles": f"profiles/{doc_id}.yaml",
    }[kind]


def plan_snapshot(fetched: dict) -> tuple:
    """Pure planner.

    fetched: {kind: {doc_id: doc_dict}} for kind in _KINDS.
    Returns (files, skipped):
      files   = {relpath: file_text} for every non-PII document
      skipped = ["kind/doc_id", ...] for PII documents the guard excluded
    """
    files: dict = {}
    skipped: list = []
    for kind in _KINDS:
        for doc_id, doc in fetched.get(kind, {}).items():
            if is_pii_doc(doc_id, doc):
                skipped.append(f"{kind}/{doc_id}")
                continue
            if kind in ("tokens_system", "tokens_user"):
                files[relpath_for(kind, doc_id)] = token_to_file(doc)
            else:
                files[relpath_for(kind, doc_id)] = doc_to_yaml(doc)
    return files, skipped
