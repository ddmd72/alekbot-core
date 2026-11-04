"""A passages repository whose chunk text read fails once for chosen chunks: `get_chunks` leaves the chunk out on its
first read, as a text missing from the index answers (`_excerpt_files` treats it as absent)."""
from __future__ import annotations

from typing import Set, Tuple

from .test_drive_search_excerpts import CountingRepo


class FlakyRepo(CountingRepo):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once: Set[Tuple[str, int]] = set()   # (item id, index): missing from its first read only

    @classmethod
    def over(cls, repo: CountingRepo) -> "FlakyRepo":
        """A flaky repository holding `repo`'s data (the world's sync state, records and chunks as stored so far)."""
        flaky = cls()
        flaky.__dict__.update({k: v for k, v in repo.__dict__.items() if k not in ("calls", "knn_limits",
                                                                                    "scoped_knn", "record_reads")})
        return flaky

    async def get_chunks(self, user_id, item_id, generation, indexes):
        rows = await super().get_chunks(user_id, item_id, generation, indexes)
        dropped = {(item_id, i) for i in indexes} & self.fail_once
        self.fail_once -= dropped
        return [c for c in rows if (c.item_id, c.index) not in dropped]
