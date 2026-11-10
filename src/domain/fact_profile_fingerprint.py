"""What a profile was made from, and whether it still holds (fact profile RFC §4.1, §4.2, §4.4).

- `build_profiler_user_message(text, metadata)`: the profiler's user message, the exact bytes the fingerprint hashes:
  `Fact:\\n<text>`, then `\\nMetadata:\\n` and one `key: value` line per key in sorted key order (strings as they are,
  every other value as compact JSON with sorted keys); no metadata section when there is no metadata.
- `profile_fingerprint(text, metadata)`: sha256 of that message. Tags, dates, state and the old domain are not input.
- `profile_derivation(registry, atlas, is_directive)`: the code-input version; a mismatch re-derives without a call.
- `classify_profile(fact, derivation)`: missing / stale_input / stale_derivation / abandoned / fresh.
- `failure_fields(...)`: the write after a failed profiling; three failures on one fingerprint abandon the fact until its
  content changes.
"""
import hashlib
import json
from dataclasses import fields
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from .entities import FactEntity
from .fact_domains import FACT_DOMAINS

# Bump when derivation or validation code changes what it stores from the same answer.
DERIVE_VERSION = "2"
ABANDON_AFTER_FAILURES = 3
_DIGEST_CHARS = 16


def metadata_value(value: Any) -> str:
    """A metadata value as the user message prints it."""
    if isinstance(value, str):
        return value
    # sort_keys: a map read back from the store may come in another key order; default=str: a stored timestamp
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def metadata_lines(metadata: Optional[Mapping[str, Any]]) -> List[str]:
    """One `key: value` line per metadata key, in sorted key order."""
    items = metadata or {}
    return [f"{key}: {metadata_value(items[key])}" for key in sorted(items)]


def build_profiler_user_message(text: str, metadata: Optional[Mapping[str, Any]]) -> str:
    message = f"Fact:\n{text}"
    lines = metadata_lines(metadata)
    if lines:
        message += "\nMetadata:\n" + "\n".join(lines)
    return message


def profile_fingerprint(text: str, metadata: Optional[Mapping[str, Any]]) -> str:
    return hashlib.sha256(build_profiler_user_message(text, metadata).encode("utf-8")).hexdigest()


def registry_digest() -> str:
    """Digest of the whole domain registry, in its order: any change to a key, row, flag or label changes it."""
    entries = [{f.name: getattr(e, f.name) for f in fields(e)} | {"labels": dict(e.labels)} for e in FACT_DOMAINS]
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def profile_derivation(registry: str, atlas: str, is_directive: bool) -> str:
    """`DERIVE_VERSION | registry digest | atlas digest | is_directive`."""
    return f"{DERIVE_VERSION}|{registry}|{atlas}|{int(is_directive)}"


class ProfileStatus(str, Enum):
    MISSING = "missing"                    # never profiled
    STALE_INPUT = "stale_input"            # text or metadata changed since: a model call
    STALE_DERIVATION = "stale_derivation"  # only code inputs changed: re-derived from profile_answer, no call
    ABANDONED = "abandoned"                # failed ABANDON_AFTER_FAILURES times on the current content
    FRESH = "fresh"


def classify_profile(fact: FactEntity, derivation: str) -> ProfileStatus:
    """The status of one fact's profile against its current text, metadata and the current `derivation`. Abandonment is
    checked first: a fact that never got a profile can be abandoned too."""
    current = profile_fingerprint(fact.text, fact.metadata)
    if fact.profile_failed_fingerprint == current and (fact.profile_failures or 0) >= ABANDON_AFTER_FAILURES:
        return ProfileStatus.ABANDONED
    if not fact.profile_fingerprint:
        return ProfileStatus.MISSING
    if fact.profile_fingerprint != current:
        return ProfileStatus.STALE_INPUT
    if fact.profile_derivation != derivation:
        return ProfileStatus.STALE_DERIVATION
    return ProfileStatus.FRESH


def failure_fields(failures: Optional[int], failed_fingerprint: Optional[str], fingerprint: str) -> Dict[str, Any]:
    """The only write after a failed profiling: the count restarts on a new fingerprint."""
    count = (failures or 0) + 1 if failed_fingerprint == fingerprint else 1
    return {"profile_failures": count, "profile_failed_fingerprint": fingerprint}
