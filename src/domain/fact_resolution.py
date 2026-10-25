"""The forward walk of `FactRepository.resolve_current`, shared by every store that implements it over its own reader.

Pure: the walk does no I/O of its own; each store passes its reader (the Firestore repository, the dry-run kit's overlay).
It lives in the domain, not in adapters/, because a top-level adapter may not import another adapter module (REQ-ARCH-08).
"""
from typing import AbstractSet, Awaitable, Callable, Optional

from .entities import FactState
from .exceptions import FactResolutionError
from .fact_lifecycle import forward_step, successor_unknown
from .stored_fact import StoredFact

FactReader = Callable[[str], Awaitable[Optional[StoredFact]]]


async def resolve_chain(
    read: FactReader, start_id: str, account_id: str, accepted: AbstractSet[FactState]
) -> Optional[StoredFact]:
    """Follow `start_id` forward through `superseded_by` (one `forward_step` per document) to the document in `accepted`.

    `read(fact_id)` returns the stored document with its change token, or None when there is none. A missing start or
    one of another account returns None; a missing successor raises FactResolutionError, as does every refusal of
    `forward_step`.
    """
    stored = await read(start_id)
    if stored is None:
        return None
    if stored.fact.account_id != account_id:
        return None  # decided before forward_step, which raises on a foreign account
    origin_lineage_id = stored.fact.lineage_id
    hops = 0
    while True:
        next_id = forward_step(stored.fact, accepted, origin_lineage_id, start_id, account_id, hops)
        if next_id is None:
            return stored
        hops += 1
        following = await read(next_id)
        if following is None:
            raise FactResolutionError(successor_unknown(start_id))
        stored = following
