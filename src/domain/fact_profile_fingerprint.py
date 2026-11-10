"""What a profile was made from, and whether it still holds (fact profile RFC §4.1, §4.2, §4.4).

- `build_profiler_user_message(text, metadata, people)`: the profiler's user message: the confirmed people block (the
  whole list, when there is one), then `Fact:\\n<text>`, then `\\nMetadata:\\n` and one `key: value` line per key in sorted
  key order (strings as they are, every other value as compact JSON with sorted keys); no metadata section when there is
  no metadata.
- `profile_fingerprint(text, metadata, named)`: sha256 of the message WITHOUT the list block, plus the block of the
  `named` entries (the list entries the fact names, `named_people`) when there are any. A fact that names no listed
  person hashes exactly what it did before the list existed, so a list edit re-profiles only the facts naming the
  edited person. Tags, dates, state and the old domain are not input.
- `profile_derivation(registry, atlas, is_directive, recording_year)`: the code-input version; a mismatch re-derives
  without a call.
- `classify_profile(fact, derivation, named)`: missing / stale_input / stale_derivation / abandoned / fresh.
- `failure_fields(...)`: the write after a failed profiling; three failures on one fingerprint abandon the fact until its
  content changes.
"""
import hashlib
import json
from dataclasses import fields
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .confirmed_people import ConfirmedPeople, ConfirmedPerson, people_named_in_texts, render_people_block
from .entities import FactEntity
from .fact_domains import FACT_DOMAINS

# Bump when derivation or validation code changes what it stores from the same answer.
DERIVE_VERSION = "3"
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


def build_profiler_user_message(text: str, metadata: Optional[Mapping[str, Any]],
                                people: Sequence[ConfirmedPerson] = ()) -> str:
    """The message the model reads: the people block (`people` is the whole confirmed list, or nothing), then the fact."""
    message = f"Fact:\n{text}"
    lines = metadata_lines(metadata)
    if lines:
        message += "\nMetadata:\n" + "\n".join(lines)
    return render_people_block(people) + message


def named_people(people: Optional[ConfirmedPeople], text: str,
                 metadata: Optional[Mapping[str, Any]]) -> Sequence[ConfirmedPerson]:
    """The list entries this fact names (a name form or a relation word in its text or metadata): the part of the list
    that belongs to the fact's fingerprint. The texts are the ones `derive_profile` checks against."""
    return people_named_in_texts(people, [text, *metadata_lines(metadata)])


def profile_fingerprint(text: str, metadata: Optional[Mapping[str, Any]],
                        named: Sequence[ConfirmedPerson] = ()) -> str:
    material = build_profiler_user_message(text, metadata)
    if named:
        material += "\n\n" + render_people_block(named)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def registry_digest() -> str:
    """Digest of the whole domain registry, in its order: any change to a key, row, flag or label changes it."""
    entries = [{f.name: getattr(e, f.name) for f in fields(e)} | {"labels": dict(e.labels)} for e in FACT_DOMAINS]
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def profile_derivation(registry: str, atlas: str, is_directive: bool, recording_year: Optional[int] = None) -> str:
    """`DERIVE_VERSION | registry digest | atlas digest | is_directive`, then `|y<year>` when the recording year is
    given: `reported_date` can be rewritten in place, and a date printed without a year follows it."""
    base = f"{DERIVE_VERSION}|{registry}|{atlas}|{int(is_directive)}"
    return base if recording_year is None else f"{base}|y{recording_year}"


class ProfileStatus(str, Enum):
    MISSING = "missing"                    # never profiled
    STALE_INPUT = "stale_input"            # text or metadata changed since: a model call
    STALE_DERIVATION = "stale_derivation"  # only code inputs changed: re-derived from profile_answer, no call
    ABANDONED = "abandoned"                # failed ABANDON_AFTER_FAILURES times on the current content
    FRESH = "fresh"


def classify_profile(fact: FactEntity, derivation: str, named: Sequence[ConfirmedPerson] = ()) -> ProfileStatus:
    """The status of one fact's profile against its current text, metadata, the list entries it names and the current
    `derivation`. Abandonment is checked first: a fact that never got a profile can be abandoned too."""
    current = profile_fingerprint(fact.text, fact.metadata, named)
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
