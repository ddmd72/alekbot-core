"""The forward walk of `FactRepository.resolve_current`, shared by every store that implements it over its own reader.

Sans-I/O: `ForwardWalk` says which document to read next and decides from what the store read; the store runs its own
`await` loop (the Firestore repository, the dry-run kit's overlay):

    walk = ForwardWalk(fact_id, account_id, accepted)
    while walk.wanted is not None:
        walk.feed(await read(walk.wanted))
    return walk.result

It lives in the domain, not in adapters/, because a top-level adapter may not import another adapter module (REQ-ARCH-08).
"""
from typing import AbstractSet, Optional

from .entities import FactState
from .exceptions import FactResolutionError
from .fact_lifecycle import forward_step, successor_unknown
from .stored_fact import StoredFact


class ForwardWalk:
    """Follows `start_id` forward through `superseded_by` (one `forward_step` per document) to the document in `accepted`.

    `wanted` is the id to read next, None when the walk is over; `feed` takes what the store read for it (the stored
    document with its change token, or None when there is none). A missing start or one of another account ends with
    `result` None; a missing successor raises FactResolutionError, as does every refusal of `forward_step`. `result` is
    the document the walk ended on."""

    def __init__(self, start_id: str, account_id: str, accepted: AbstractSet[FactState]) -> None:
        self._start_id = start_id
        self._account_id = account_id
        self._accepted = accepted
        self._started = False
        self._origin_lineage_id = ""
        self._hops = 0
        self._wanted: Optional[str] = start_id
        self._result: Optional[StoredFact] = None

    @property
    def wanted(self) -> Optional[str]:
        return self._wanted

    @property
    def result(self) -> Optional[StoredFact]:
        return self._result

    def feed(self, stored: Optional[StoredFact]) -> None:
        if self._wanted is None:
            raise ValueError("the walk is over; there is nothing to feed")
        first = not self._started
        self._started = True
        self._wanted = None
        if stored is None:
            if first:
                return
            raise FactResolutionError(successor_unknown(self._start_id))
        if first:
            if stored.fact.account_id != self._account_id:
                return  # decided before forward_step, which raises on a foreign account
            self._origin_lineage_id = stored.fact.lineage_id
        next_id = forward_step(
            stored.fact, self._accepted, self._origin_lineage_id, self._start_id, self._account_id, self._hops
        )
        if next_id is None:
            self._result = stored
            return
        self._hops += 1
        self._wanted = next_id
