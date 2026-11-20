"""BiographicalCacheRefresher — rebuilds an account's cached biographical context after a fact write."""
from abc import ABC, abstractmethod
from typing import Optional


class BiographicalCacheRefresher(ABC):
    """The rebuild every fact-write path runs so the next prompt reads the account's current facts."""

    @abstractmethod
    async def refresh_cache(
        self,
        owner_id: str,
        facts_limit: Optional[int] = None,
        principles_limit: Optional[int] = None,
    ) -> None:
        """Rebuild the cached biographical context of account `owner_id` from its current facts.

        Content: up to facts_limit facts in get_active_facts_ordered order, the identity block
            (profile user_identity == true) first, then the other non-directive facts; each item
            carries the fact's primary_domain and sensitivity; of these, facts tagged "mindset" become
            principles (they count toward facts_limit; those beyond principles_limit are dropped);
            directive facts (`directive == true`) separately, up to 15 (DEFAULT_DIRECTIVES_CACHE_LIMIT).
        Limit: facts_limit and principles_limit are what the cache holds; an explicit value wins over the
            context service's own resolution (account defaults, then system default), None keeps that resolution.
        Idempotency: refreshing an unchanged account writes the same content.
        Errors: never raises; on a failure the previous cache stays (observed).
        """
