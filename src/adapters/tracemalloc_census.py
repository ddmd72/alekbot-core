"""MemoryCensusPort over ``tracemalloc`` (diagnostic only; see ports/memory_census.py).

``tracemalloc`` records the allocation site of every live block: exact attribution, at the price of
a slower process and extra memory for the bookkeeping. It is therefore started only when the owner
sets ``MEMORY_TRACEMALLOC`` for an investigation window.
"""
import ctypes
import gc
import tracemalloc
from typing import List, Tuple

from src.domain.memory_telemetry import CensusReport, CensusSite, ReleaseResult
from src.ports.memory_census import MemoryCensusPort
from src.utils.logger import logger

_STACK_SITES = 5          # the heaviest sites also get a short call stack
_STACK_DEPTH = 6
_TRACEMALLOC_OWN = tracemalloc.__file__


def _fmt(frame: "tracemalloc.Frame") -> str:
    return f"{_short(frame.filename)}:{frame.lineno}"


def _short(path: str) -> str:
    """Keep the part of the path that identifies the module (site-packages/… or src/…)."""
    for marker in ("/site-packages/", "/src/"):
        if marker in path:
            keep = path.split(marker, 1)[1]
            return keep if marker == "/site-packages/" else "src/" + keep
    return path.rsplit("/", 2)[-2] + "/" + path.rsplit("/", 1)[-1] if "/" in path else path


class TracemallocCensus(MemoryCensusPort):
    def __init__(self, frames: int = 4) -> None:
        self._frames = max(1, frames)

    def start(self) -> None:
        if not tracemalloc.is_tracing():
            tracemalloc.start(self._frames)
            logger.warning("MemoryCensus: tracemalloc started (frames=%s): expect a slower process and extra memory",
                           self._frames)

    def stop(self) -> None:
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    def snapshot(self, top_n: int) -> CensusReport:
        if not tracemalloc.is_tracing():
            return CensusReport(traced_bytes=0, peak_bytes=0, sites=())
        current, peak = tracemalloc.get_traced_memory()
        snap = tracemalloc.take_snapshot().filter_traces((
            tracemalloc.Filter(False, _TRACEMALLOC_OWN),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        ))
        by_line = snap.statistics("lineno")[:top_n]
        by_trace = snap.statistics("traceback")[:_STACK_SITES]
        stacks = {self._key(t.traceback[-1]): self._stack(t.traceback) for t in by_trace if t.traceback}
        sites: List[CensusSite] = []
        for stat in by_line:
            frame = stat.traceback[0]
            sites.append(CensusSite(
                site=_fmt(frame), size_bytes=stat.size, count=stat.count,
                stack=stacks.get(self._key(frame), ()),
            ))
        return CensusReport(traced_bytes=current, peak_bytes=peak, sites=tuple(sites))

    @staticmethod
    def _key(frame: "tracemalloc.Frame") -> Tuple[str, int]:
        return (frame.filename, frame.lineno)

    @staticmethod
    def _stack(tb: "tracemalloc.Traceback") -> Tuple[str, ...]:
        frames = list(tb)[-_STACK_DEPTH:]
        return tuple(_fmt(f) for f in frames)

    def release(self) -> ReleaseResult:
        collected = gc.collect()
        trimmed = False
        try:
            trimmed = bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
        except (OSError, AttributeError):
            logger.info("MemoryCensus: malloc_trim unavailable on this libc")
        return ReleaseResult(gc_collected=collected, trimmed=trimmed)
