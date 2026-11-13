"""
InMemoryEmailCardIndex — EmailCardIndex in process memory. The first of the port's two implementations (the second is
Firestore, slice 8): the unit tests and the snapshot harness (`scripts/validation/email_eval`) run the real search on
it. kNN is brute-force cosine; the argument errors are the port's ValueErrors. Reads take no lock: a read and the write
that follows it have no await between them (one event loop).

Only `carded` cards are read; a card of another user is never visible. `add_uncarded` records a stored mail that has
no settled card yet; a card `add`ed for the same id (or in state `pending`) is counted by `count_uncarded` until it is
carded.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..domain.drive_index_params import CONCEPT_KEYS_MAX
from ..domain.email_card import CardState, EmailCard
from ..ports.email_card_index import KEY_FIELDS, EmailCardIndex


class InMemoryEmailCardIndex(EmailCardIndex):
    def __init__(self) -> None:
        self._cards: Dict[str, Dict[str, Tuple[EmailCard, Optional[Tuple[float, ...]]]]] = {}
        self._uncarded: Dict[str, Set[str]] = {}

    # -- test and harness setup -------------------------------------------------------------------------------------

    def add(self, user_id: str, card: EmailCard, vector: Optional[Sequence[float]] = None) -> None:
        """Store `card` (any state) under `user_id`; it replaces an earlier card of the same mail."""
        self._cards.setdefault(user_id, {})[card.mail.email_id] = (card, tuple(vector) if vector is not None else None)
        self._uncarded.get(user_id, set()).discard(card.mail.email_id)

    def add_uncarded(self, user_id: str, email_id: str) -> None:
        """Record a stored mail of `user_id` that has no card fields yet."""
        self._uncarded.setdefault(user_id, set()).add(email_id)

    # -- the port ---------------------------------------------------------------------------------------------------

    async def find_by_keys(self, user_id: str, field: str, keys: Sequence[str], limit: int) -> List[EmailCard]:
        if field not in KEY_FIELDS:
            raise ValueError(f"unknown key field {field!r}")
        wanted = _bounded(keys, "keys")
        return [c for c, _ in self._carded(user_id) if wanted.intersection(getattr(c, field))][:max(limit, 0)]

    async def list_in_months(self, user_id: str, months: Sequence[str], limit: int) -> List[EmailCard]:
        wanted = _bounded(months, "months")
        return [c for c, _ in self._carded(user_id) if wanted.intersection(c.month_keys)][:max(limit, 0)]

    async def find_nearest(self, user_id: str, vector: Sequence[float], limit: int) -> List[Tuple[EmailCard, float]]:
        scored = [(c, _cosine_distance(vector, v)) for c, v in self._carded(user_id) if v is not None]
        scored.sort(key=lambda pair: (pair[1], pair[0].mail.email_id))
        return scored[:max(limit, 0)]

    async def count_carded(self, user_id: str) -> int:
        return len(self._carded(user_id))

    async def count_uncarded(self, user_id: str) -> int:
        pending = {c.mail.email_id for c, _ in self._cards.get(user_id, {}).values() if c.state is CardState.PENDING}
        return len(pending | self._uncarded.get(user_id, set()))

    # -- implementation ---------------------------------------------------------------------------------------------

    def _carded(self, user_id: str) -> List[Tuple[EmailCard, Optional[Tuple[float, ...]]]]:
        """The user's carded cards in a stable order (by mail id)."""
        stored = self._cards.get(user_id, {})
        return [stored[i] for i in sorted(stored) if stored[i][0].state is CardState.CARDED]


def _bounded(values: Sequence[str], what: str) -> Set[str]:
    if len(values) > CONCEPT_KEYS_MAX:
        raise ValueError(f"at most {CONCEPT_KEYS_MAX} {what} per read, got {len(values)}")
    return set(values)


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / norm if norm else 1.0
