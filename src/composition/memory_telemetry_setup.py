"""Wire the memory telemetry service for one process (decisions/memory_telemetry.md).

Every entry point (main, indexer, relay, research job) calls ``build_memory_telemetry`` once and
``await start()`` / ``await stop()`` around its lifetime. Env knobs are optional (``os.getenv``, not
``load_settings()``): ``MEMORY_TELEMETRY`` (default on; ``off``/``0``/``false`` disables) and
``MEMORY_SAMPLE_INTERVAL_S``.
"""
import os
from typing import Optional

from src.adapters.proc_memory_probe import ProcMemoryProbe
from src.domain.memory_telemetry import BASE_SAMPLE_INTERVAL_S
from src.services.memory_telemetry_service import MemoryTelemetryService
from src.utils.logger import logger

_OFF = {"off", "0", "false", "no"}
_MIN_INTERVAL_S = 1.0


def memory_telemetry_enabled() -> bool:
    return os.getenv("MEMORY_TELEMETRY", "on").strip().lower() not in _OFF


def sample_interval_from_env() -> float:
    raw = os.getenv("MEMORY_SAMPLE_INTERVAL_S", "").strip()
    if not raw:
        return BASE_SAMPLE_INTERVAL_S
    try:
        return max(_MIN_INTERVAL_S, float(raw))
    except ValueError:
        logger.warning("MEMORY_SAMPLE_INTERVAL_S=%r is not a number; using %ss", raw, BASE_SAMPLE_INTERVAL_S)
        return BASE_SAMPLE_INTERVAL_S


def build_memory_telemetry(default_service: str) -> Optional[MemoryTelemetryService]:
    """The service for this process, or None when switched off."""
    if not memory_telemetry_enabled():
        logger.info("MemoryTelemetry disabled (MEMORY_TELEMETRY=%s)", os.getenv("MEMORY_TELEMETRY"))
        return None
    service = os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB") or default_service
    revision = os.getenv("K_REVISION") or os.getenv("CLOUD_RUN_EXECUTION") or "local"
    return MemoryTelemetryService(
        ProcMemoryProbe(), service=service, revision=revision, base_interval_s=sample_interval_from_env(),
    )
