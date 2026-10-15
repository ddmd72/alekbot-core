"""Port: look at the memory of this process, its children and its container (OS boundary)."""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from src.domain.memory_telemetry import ProbeReading


class MemoryProbePort(ABC):
    """One implementation reads /proc and cgroups; tests substitute a fake."""

    @abstractmethod
    def read(self) -> ProbeReading:
        """Current memory figures. Must be cheap (called every second under pressure) and must
        never raise: anything unreadable is reported as unknown, not as an error."""

    @abstractmethod
    def usage_bytes(self) -> Optional[int]:
        """Container usage only — a single file read, cheap enough for every work-unit boundary."""

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """What this runtime lets us read (cgroup layout, limit, which sources exist), logged once
        at start so an unfamiliar environment explains itself."""
