"""
The fact profile step's wiring and knob (docs/10_rfcs/FACT_PROFILE_RFC.md §4.4).

    max_per_run(raw) -> int                      the `FACT_PROFILER_MAX_PER_RUN` knob: unset or blank 20, an int >= 0
                                                 as given (0 = re-derive only), anything else 20 and one WARNING
    build_fact_profile_service(container, max_per_run=...) -> FactProfileService

The knob is an optional one, read with `os.getenv` by the caller (never in services/, never in `load_settings()`). The
place atlas is built lazily on the first run, off the event loop (about 0.9 s and 32 MB retained, src/assets/geo/
README.md), and kept for the process: never at boot.
"""
import asyncio
import threading
from typing import Any, Optional, Tuple

from ..adapters.geo_place_table import load_place_rows
from ..domain.place_names import PlaceNames, atlas_digest
from ..infrastructure.agent_config import FACT_PROFILER
from ..ports.person_card_repository import PersonCardRepository
from ..ports.quota_service import QuotaService
from ..services.fact_profile_service import FactProfileService
from ..utils.logger import logger

DEFAULT_MAX_PER_RUN = 20
KNOB = "FACT_PROFILER_MAX_PER_RUN"


def max_per_run(raw: Optional[str]) -> int:
    """The per-run cap of model calls from the knob's raw value."""
    value = (raw or "").strip()
    if not value:
        return DEFAULT_MAX_PER_RUN
    try:
        parsed = int(value)
    except ValueError:
        parsed = -1
    if parsed < 0:
        logger.warning(f"[FactProfile] {KNOB} is not an integer >= 0; using {DEFAULT_MAX_PER_RUN}")
        return DEFAULT_MAX_PER_RUN
    return parsed


class LazyAtlas:
    """The place names and their digest, loaded once (concurrent first calls share one load). The service loads it at
    boot (`warm`); a failed boot load leaves the first use to load it. The fact search reads the same object through
    `names`, so the process holds one copy of the table."""

    def __init__(self) -> None:
        # A threading lock, not asyncio.Lock: the load runs in a worker thread (`names` is called from the fact
        # search's own to_thread), where an asyncio lock cannot be taken.
        self._lock = threading.Lock()
        self._loaded: Optional[Tuple[PlaceNames, str]] = None

    def _load(self) -> Tuple[PlaceNames, str]:
        with self._lock:
            if self._loaded is None:
                rows = load_place_rows()
                self._loaded = (PlaceNames(rows), atlas_digest(rows))
            return self._loaded

    async def __call__(self) -> Tuple[PlaceNames, str]:
        return await asyncio.to_thread(self._load)

    def names(self) -> PlaceNames:
        """Blocking: the place names, loading the table on the first call. Call it off the event loop."""
        return self._load()[0]

    async def warm(self) -> None:
        """Load the table now, off the event loop. A failure is logged and swallowed: the first use loads it."""
        try:
            await self()
        except Exception:
            logger.error("[PlaceTable] boot load failed, the first use loads it lazily", exc_info=True)


def build_fact_profile_service(
    container: Any, *, max_per_run: Optional[int], person_cards: Optional[PersonCardRepository] = None,
    atlas: Optional[LazyAtlas] = None, quota: Optional[QuotaService] = None,
) -> FactProfileService:
    """The profile step over the container's repository, profiler and embedding service; `person_cards` is where it
    rebuilds the people cards (none: it builds no cards). `atlas` is the place atlas to share with the owner's word
    replies (one atlas per process); none: the step loads its own. `quota` is the graph's quota service, which bills the
    step's label embeddings; none: they are not billed (and logged so)."""
    return FactProfileService(
        repo=container.repository,
        profiler=container.fact_profiler,
        embeddings=container.embedding_service,
        atlas=atlas if atlas is not None else LazyAtlas(),
        max_per_run=max_per_run,
        call_budget_s=2 * FACT_PROFILER.call_timeout_s,  # one parse retry
        person_cards=person_cards,
        confirmed_people=container.confirmed_people,
        profile_answers=container.profile_answers,
        quota=quota,
    )
