"""CabinetFactService — the owner's writes to his facts from the Cabinet (today: the delete of one fact)."""
from datetime import datetime, timezone

from ..domain.exceptions import FactWriteConflict
from ..domain.fact_lifecycle import CABINET_ACCEPTS, cabinet_invalidation
from ..ports.repository import FactRepository
from ..utils.logger import logger


class CabinetFactService:
    """The Cabinet's fact writes, through the fact repository port."""

    def __init__(self, repo: FactRepository) -> None:
        self._repo = repo

    async def invalidate(self, fact_id: str, account_id: str) -> None:
        """Mark `fact_id` invalidated, following a superseded id to its current version, then refresh the cached context.

        Tenancy: the stored account_id of `fact_id` must equal `account_id`.
        Resolution: resolve_current with CABINET_ACCEPTS (every state but superseded). The document the walk ends on is
            invalidated, so a stale id from an open page still reaches the fact, but only when its text equals the text
            of `fact_id` (what the page showed); a newer text raises FactResolutionError.
        Effect: state invalidated, is_current True, valid_to and last_updated now; version unchanged; one
            `lifecycle_events` entry (actor "cabinet", reason "deleted in the Cabinet"). Then the biographical cache of
            the account is refreshed (the repository logs and swallows a refresh failure), so the fact leaves the prompt.
        Errors: a missing id raises ValueError; a fact of another account raises PermissionError, here or from the
            write's tenancy precondition on the stored account_id (a legacy or stray `owner_id`); FactResolutionError
            when the chain ends in a merge, has no successor or ends on a different text (`current_id` names that
            version); FactWriteConflict after one re-resolve that also conflicted. Nothing is written and the cache is
            not refreshed in any of these cases.
        """
        shown = await self._repo.get_fact_by_id(fact_id)
        if shown is None:
            raise ValueError(f"Fact not found: {fact_id}")
        if shown.account_id != account_id:
            raise PermissionError("Not authorized to modify this fact")
        for attempt in (1, 2):
            stored = await self._repo.resolve_current(fact_id, account_id, CABINET_ACCEPTS)
            if stored is None:
                raise ValueError(f"Fact not found: {fact_id}")
            fields = cabinet_invalidation(shown.text, stored.fact, fact_id, datetime.now(timezone.utc))
            try:
                await self._repo.update_fact_fields(stored, fields)
                break
            except FactWriteConflict:
                logger.warning(f"[FactRepo] invalidate of {fact_id} conflicted (attempt {attempt})")
                if attempt == 2:
                    raise
        await self._repo.refresh_biographical_context_cache(owner_id=account_id)
