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
from typing import Any, Optional, Tuple

from ..adapters.geo_place_table import load_place_rows
from ..domain.place_names import PlaceNames, atlas_digest
from ..infrastructure.agent_config import FACT_PROFILER
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
    """The place names and their digest, loaded once on first use (concurrent first calls share one load)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._loaded: Optional[Tuple[PlaceNames, str]] = None

    async def __call__(self) -> Tuple[PlaceNames, str]:
        async with self._lock:
            if self._loaded is None:
                rows = await asyncio.to_thread(load_place_rows)
                self._loaded = (PlaceNames(rows), atlas_digest(rows))
            return self._loaded


def build_fact_profile_service(container: Any, *, max_per_run: Optional[int]) -> FactProfileService:
    """The profile step over the container's repository, profiler and embedding service."""
    return FactProfileService(
        repo=container.repository,
        profiler=container.fact_profiler,
        embeddings=container.embedding_service,
        atlas=LazyAtlas(),
        max_per_run=max_per_run,
        call_budget_s=2 * FACT_PROFILER.call_timeout_s,  # one parse retry
    )
