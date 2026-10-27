"""Inputs the P2 reopen script and bench 7 read from `scripts/memory/`: a file of fact ids, and a classify plan's expiry fields.

The plan reader is a minimal local one for the documented JSON of the S5 classify plan (`kind` "fact_s5_classify_plan",
`version` 1, `decisions[]` with `fact_id` and `fields` {expiry_kind, ttl_days, expires_at}); the S5 module itself is not on
this branch, so nothing is imported from it. It reads only what the callers apply and refuses a malformed plan."""
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.domain.entities import ExpiryKind

PLAN_KIND = "fact_s5_classify_plan"
PLAN_VERSION = 1
MEMORY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "memory"


class SelectionError(ValueError):
    """An input file is outside scripts/memory/, unreadable or malformed."""


def inside_memory(path: Path, memory_dir: Optional[Path] = None) -> Path:
    """`path` resolved; it must sit under `memory_dir` (default `MEMORY_DIR`, read at call time; ids and plans are private)."""
    base = memory_dir or MEMORY_DIR
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise SelectionError(f"{path.name}: the file must be under {base}")
    return resolved


def read_ids(path: Path, memory_dir: Optional[Path] = None) -> List[str]:
    """The ids of `path` (one per line, `#` comments and blanks ignored), in order and without duplicates."""
    ids: Dict[str, None] = {}
    for line in inside_memory(path, memory_dir).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids[line] = None
    return list(ids)


def read_expiry_plan(path: Path, memory_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """fact id -> {expiry_kind: ExpiryKind, ttl_days: Optional[int], expires_at: Optional[aware datetime]}."""
    try:
        doc = json.loads(inside_memory(path, memory_dir).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SelectionError("the plan is not JSON") from exc
    if not isinstance(doc, Mapping) or doc.get("kind") != PLAN_KIND or doc.get("version") != PLAN_VERSION:
        raise SelectionError("not a classify plan of the supported version")
    items = doc.get("decisions")
    if not isinstance(items, list):
        raise SelectionError("the plan has no decisions list")
    plan: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(items):
        fact_id = item.get("fact_id") if isinstance(item, Mapping) else None
        raw = item.get("fields") if isinstance(item, Mapping) else None
        if not isinstance(fact_id, str) or not fact_id or not isinstance(raw, Mapping):
            raise SelectionError(f"decision {index}: fact_id or fields missing")
        if fact_id in plan:
            raise SelectionError(f"decision {index}: duplicate fact_id")
        plan[fact_id] = _fields(index, raw)
    return plan


def _fields(index: int, raw: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        kind = ExpiryKind(raw.get("expiry_kind"))
    except ValueError:
        raise SelectionError(f"decision {index}: unknown expiry_kind") from None
    ttl_days = raw.get("ttl_days")
    if ttl_days is not None and (not isinstance(ttl_days, int) or isinstance(ttl_days, bool)):
        raise SelectionError(f"decision {index}: ttl_days is not a whole number")
    expires_at: Optional[datetime] = None
    if raw.get("expires_at") is not None:
        try:
            expires_at = datetime.fromisoformat(raw["expires_at"])
        except (TypeError, ValueError):
            raise SelectionError(f"decision {index}: expires_at is not a datetime") from None
        if expires_at.tzinfo is None:
            raise SelectionError(f"decision {index}: expires_at is not timezone-aware")
    return {"expiry_kind": kind, "ttl_days": ttl_days, "expires_at": expires_at}
