"""Memory telemetry: sample the process and account for work units (decisions/memory_telemetry.md).

Public interface
----------------
``MemoryTelemetryService(probe, service, revision, base_interval_s, emit)``
  * ``await start()`` / ``await stop()`` — run the sampler loop; install / remove the scope hooks.
  * ``register_contributor(name, fn)`` — a named number (MB or count) added to every sample, for
    figures only a component can know (attachment bytes in flight, cache sizes).
  * ``sample_once()`` — one ``memory.sample`` record (the loop calls it; tests call it directly).

Every record is a flat dict emitted as ``jsonPayload`` through the logger, so Cloud Logging and the
BigQuery sink carry it with no write path of our own. Telemetry never raises into the caller: a
failed probe or contributor is logged once and skipped.
"""
import asyncio
import os
import platform
import uuid
from typing import Any, Callable, Dict, Optional, Set

from src.domain.memory_telemetry import (
    BASE_SAMPLE_INTERVAL_S,
    EVENT_BOOT,
    ProbeReading,
    ScopeRecord,
    sample_fields,
    sample_interval_s,
    scope_fields,
    usage_pct,
)
from src.ports.memory_probe import MemoryProbePort
from src.utils import memory_scope
from src.utils.logger import logger

Emit = Callable[[str, Dict[str, Any]], None]


def log_emit(message: str, fields: Dict[str, Any]) -> None:
    """Default sink: one structured log line (``json_fields`` becomes ``jsonPayload``)."""
    logger.info(message, extra={"json_fields": fields})


class MemoryTelemetryService:
    def __init__(
        self,
        probe: MemoryProbePort,
        service: str,
        revision: str,
        base_interval_s: float = BASE_SAMPLE_INTERVAL_S,
        emit: Emit = log_emit,
    ) -> None:
        self._probe = probe
        self._service = service
        self._revision = revision
        self._base_interval_s = base_interval_s
        self._emit = emit
        self._boot = uuid.uuid4().hex[:8]
        self._contributors: Dict[str, Callable[[], float]] = {}
        self._warned: Set[str] = set()
        self._task: Optional["asyncio.Task[None]"] = None
        self._last_pct: Optional[float] = None

    # -- configuration -----------------------------------------------------

    def register_contributor(self, name: str, fn: Callable[[], float]) -> None:
        self._contributors[name] = fn

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._emit_boot()
        memory_scope.install_hooks(self)
        self._task = asyncio.create_task(self._run(), name="memory-telemetry")

    async def stop(self) -> None:
        task, self._task = self._task, None
        memory_scope.uninstall_hooks()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # -- scope hooks (memory_scope.ScopeHooks) ------------------------------

    def usage_bytes(self) -> Optional[int]:
        try:
            return self._probe.usage_bytes()
        except Exception as e:  # telemetry must not break the work it measures
            self._warn_once("usage_bytes", e)
            return None

    def on_scope_end(self, record: ScopeRecord) -> None:
        fields = self._with_identity(scope_fields(record))
        self._safe_emit(f"memory.scope {record.kind}/{record.name} {fields.get('dur_s')}s "
                        f"delta={fields.get('delta_mb')}MB", fields)

    # -- sampling ----------------------------------------------------------

    def sample_once(self) -> Optional[ProbeReading]:
        try:
            reading = self._probe.read()
        except Exception as e:
            self._warn_once("read", e)
            return None
        memory_scope.note_usage(reading.container.usage_bytes if reading.container else None)
        self._last_pct = usage_pct(reading.container)
        fields = self._with_identity(sample_fields(
            reading, memory_scope.active_scopes(), self._read_contributors(),
        ))
        fields.update(self._runtime_counts())
        self._safe_emit(f"memory.sample usage={fields.get('usage_mb')}MB rss={fields.get('rss_mb')}MB "
                        f"scopes={fields.get('n_scopes')}", fields)
        return reading

    async def _run(self) -> None:
        while True:
            self.sample_once()
            await asyncio.sleep(sample_interval_s(self._last_pct, self._base_interval_s))

    # -- internals ---------------------------------------------------------

    def _with_identity(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        fields["svc"] = self._service
        fields["rev"] = self._revision
        fields["boot"] = self._boot
        return fields

    @staticmethod
    def _runtime_counts() -> Dict[str, Any]:
        try:
            return {"tasks": len(asyncio.all_tasks())}
        except RuntimeError:  # no running loop (direct call from a test or a thread)
            return {}

    def _read_contributors(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for name, fn in self._contributors.items():
            try:
                out[name] = float(fn())
            except Exception as e:
                self._warn_once(f"contributor:{name}", e)
        return out

    def _emit_boot(self) -> None:
        try:
            described = self._probe.describe()
        except Exception as e:
            self._warn_once("describe", e)
            described = {}
        fields = self._with_identity({
            "event": EVENT_BOOT,
            "interval_s": self._base_interval_s,
            "pid": os.getpid(),
            "python": platform.python_version(),
            **{f"probe_{k}": v for k, v in described.items()},
        })
        self._safe_emit(f"memory.boot {self._service} rev={self._revision} cgroup={described.get('cgroup')}", fields)

    def _safe_emit(self, message: str, fields: Dict[str, Any]) -> None:
        try:
            self._emit(message, fields)
        except Exception as e:
            self._warn_once("emit", e)

    def _warn_once(self, key: str, error: Exception) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning("MemoryTelemetry: %s failed (%s: %s); skipping from now on", key, type(error).__name__, error)
