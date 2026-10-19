"""Port: attribute held memory to the code that allocated it (diagnostic, opt-in, slow)."""
from abc import ABC, abstractmethod

from src.domain.memory_telemetry import CensusReport, ReleaseResult


class MemoryCensusPort(ABC):
    """Process-introspection boundary. The one implementation uses ``tracemalloc``, which makes the
    process run noticeably slower and hold bookkeeping memory, so it is switched on only for an
    investigation window (``MEMORY_TRACEMALLOC``)."""

    @abstractmethod
    def start(self) -> None:
        """Begin tracing allocations. Idempotent."""

    @abstractmethod
    def stop(self) -> None:
        """Stop tracing and drop the bookkeeping. Idempotent."""

    @abstractmethod
    def snapshot(self, top_n: int) -> CensusReport:
        """Heaviest allocation sites still alive. May take seconds; call from a worker thread."""

    @abstractmethod
    def release(self) -> ReleaseResult:
        """Run a garbage collection and ask the allocator to return free pages to the OS."""
