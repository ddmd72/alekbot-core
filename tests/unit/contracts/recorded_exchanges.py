"""The committed recordings of provider exchanges, for the recorded-exchange tests (P17).

`tests/fixtures/providers/<provider>/<scenario>.json`, written only by `make contract-record`
(`scripts/contracts/record.py`) and discovered from the folder: a new recording is covered by
`test_recorded_exchanges.py` and `test_recorded_exchanges_replay.py` without editing them.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from scripts.contracts import record as rec

RECORDINGS = sorted(rec.FIXTURES_DIR.glob("*/*.json"))


class PreconditionFailed(Exception):
    """Not an AssertionError: a broken setup inside a strict xfail reports FAILED, never a silent XFAIL."""


def require(cond: bool, what: str) -> None:
    if not cond:
        raise PreconditionFailed(what)


@functools.lru_cache(maxsize=None)
def recording(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def recording_id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


def provider_of(path: Path) -> str:
    return recording(path)["_meta"]["provider"]


# Every exchange of every recording, as (recording, its index).
EXCHANGES = [(p, n) for p in RECORDINGS for n in range(len(recording(p)["exchanges"]))]
