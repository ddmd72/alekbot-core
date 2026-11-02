"""The Google Drive change cursor (GOOGLE_DRIVE_RFC §3.3): the adapter's own opaque JSON, bound to the drive identity.

```
{"v": 1, "drive": "<drive_identity>", "phase": "enumerate" | "feed",
 "start": "<changes start token>", "corpora": [...], "list": "<files.list page token>", "page": "<changes page token>"}
```

- `phase="enumerate"`: the emulated full enumeration. `start` is the `changes.getStartPageToken` taken before it ("" on
  a restart cursor: the next call takes it); `corpora` holds the passes left, each `{"c": "user"}` or
  `{"c": "drive", "id": <driveId>, "name": <drive name from drives.list>}`, the first being the pass in progress
  (None on a restart cursor: the next call lists the member drives); `list` is that pass's `files.list` page token
  ("" = its first page).
- `phase="feed"`: `page` is the `changes.list` page token.

`decode` raises ValueError for anything this module did not write (unreadable JSON, a wrong `v`, a missing or mistyped
field); the adapter turns that into `DriveResyncRequired(<fresh cursor>)`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

CURSOR_VERSION = 1
ENUMERATE = "enumerate"
FEED = "feed"

Corpus = Dict[str, str]


@dataclass(frozen=True)
class FeedCursor:
    drive: str
    phase: str = ENUMERATE
    start: str = ""
    corpora: Optional[Tuple[Corpus, ...]] = None
    list_token: str = ""
    page: str = ""
    v: int = field(default=CURSOR_VERSION)

    @property
    def fresh(self) -> bool:
        """A restart cursor: the next call takes the start token and lists the member drives."""
        return self.phase == ENUMERATE and (not self.start or self.corpora is None)

    def with_list(self, token: str) -> FeedCursor:
        return replace(self, list_token=token)

    def next_pass(self) -> FeedCursor:
        """The pass in progress ended: drop it, start the next one at its first page."""
        return replace(self, corpora=tuple(self.corpora or ())[1:], list_token="")

    def to_feed(self) -> FeedCursor:
        """The enumeration ended: the feed starts at `start`, so it replays what changed meanwhile (§3.3)."""
        return FeedCursor(drive=self.drive, phase=FEED, start=self.start, corpora=None, list_token="", page=self.start)

    def with_page(self, token: str) -> FeedCursor:
        return replace(self, page=token)


def restart(drive: str) -> FeedCursor:
    """A fresh enumeration cursor of `drive`."""
    return FeedCursor(drive=drive)


def encode(cursor: FeedCursor) -> str:
    data: Dict[str, Any] = {"v": cursor.v, "drive": cursor.drive, "phase": cursor.phase, "start": cursor.start,
                            "corpora": None if cursor.corpora is None else [dict(c) for c in cursor.corpora],
                            "list": cursor.list_token, "page": cursor.page}
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _corpus(value: Any) -> Corpus:
    if not isinstance(value, dict):
        raise ValueError("a corpus is an object")
    kind = value.get("c")
    if kind == "user" and set(value) == {"c"}:
        return {"c": "user"}
    if kind == "drive" and isinstance(value.get("id"), str) and value["id"] and isinstance(value.get("name"), str):
        return {"c": "drive", "id": value["id"], "name": value["name"]}
    raise ValueError("an unknown corpus")


def decode(raw: str) -> FeedCursor:
    """The cursor `encode` wrote; ValueError for anything else."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor is not JSON") from exc
    if not isinstance(data, dict) or data.get("v") != CURSOR_VERSION:
        raise ValueError("cursor of another version")
    strings = ("drive", "phase", "start", "list", "page")
    if not all(isinstance(data.get(name), str) for name in strings):
        raise ValueError("cursor field missing or mistyped")
    if data["phase"] not in (ENUMERATE, FEED) or not data["drive"]:
        raise ValueError("cursor phase or drive unreadable")
    corpora_raw = data.get("corpora")
    corpora: Optional[Tuple[Corpus, ...]] = None
    if corpora_raw is not None:
        if not isinstance(corpora_raw, list):
            raise ValueError("cursor corpora mistyped")
        corpora = tuple(_corpus(c) for c in corpora_raw)
    if data["phase"] == FEED and not data["page"]:
        raise ValueError("a feed cursor without a page token")
    return FeedCursor(drive=data["drive"], phase=data["phase"], start=data["start"], corpora=corpora,
                      list_token=data["list"], page=data["page"])


def corpora_of(drives: List[Tuple[str, str]]) -> Tuple[Corpus, ...]:
    """The passes of an enumeration: the user corpus, then one per member shared drive `(id, name)` (GE-14)."""
    return ({"c": "user"},) + tuple({"c": "drive", "id": drive_id, "name": name} for drive_id, name in drives)
