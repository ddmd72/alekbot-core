"""MemoryProbePort over /proc and the cgroup filesystem (no psutil: nothing new to pin).

Cloud Run exposes cgroup **v1** (probed 2026-10-15, gen2: ``memory/memory.usage_in_bytes``); v2 paths
are tried first so a future runtime keeps working. Container usage is what the platform meters and
what kills the instance — it includes the in-memory writable layer, which RSS does not.
"""
import os
import time
from typing import Any, Dict, List, Optional, Set

from src.domain.memory_telemetry import ChildProcess, ContainerMemory, ProbeReading
from src.ports.memory_probe import MemoryProbePort

_UNLIMITED = 1 << 60  # cgroup v1 reports "no limit" as a number near 2**63
_MAX_CHILDREN_SCANNED = 64


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    text = _read_text(path)
    if text is None:
        return None
    text = text.strip()
    if not text.isdigit():  # cgroup v2 writes the word "max" for no limit
        return None
    return int(text)


def _kv(text: Optional[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _status_bytes(text: Optional[str], key: str) -> Optional[int]:
    for line in (text or "").splitlines():
        if line.startswith(key + ":"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1]) * 1024
    return None


def _status_int(text: Optional[str], key: str) -> Optional[int]:
    for line in (text or "").splitlines():
        if line.startswith(key + ":"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1])
    return None


class ProcMemoryProbe(MemoryProbePort):
    """Roots are injectable so tests can build a fake /proc and cgroup tree."""

    def __init__(
        self,
        proc_root: str = "/proc",
        cgroup_root: str = "/sys/fs/cgroup",
        tmp_path: str = "/tmp",
        pid: Optional[int] = None,
    ) -> None:
        self._proc = proc_root
        self._cg = cgroup_root
        self._tmp = tmp_path
        self._pid = pid if pid is not None else os.getpid()
        try:
            self._ticks = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError, AttributeError):
            self._ticks = 100

    # -- container ---------------------------------------------------------

    def _container(self) -> Optional[ContainerMemory]:
        v2 = _read_int(os.path.join(self._cg, "memory.current"))
        if v2 is not None:
            stat = _kv(_read_text(os.path.join(self._cg, "memory.stat")))
            return ContainerMemory(
                usage_bytes=v2,
                limit_bytes=_read_int(os.path.join(self._cg, "memory.max")),
                cache_bytes=stat.get("file"),
            )
        v1_dir = os.path.join(self._cg, "memory")
        v1 = _read_int(os.path.join(v1_dir, "memory.usage_in_bytes"))
        if v1 is None:
            return None
        limit = _read_int(os.path.join(v1_dir, "memory.limit_in_bytes"))
        if limit is not None and limit >= _UNLIMITED:
            limit = None
        stat = _kv(_read_text(os.path.join(v1_dir, "memory.stat")))
        return ContainerMemory(usage_bytes=v1, limit_bytes=limit, cache_bytes=stat.get("cache"))

    # -- processes ---------------------------------------------------------

    def _uptime_s(self) -> Optional[float]:
        text = _read_text(os.path.join(self._proc, "uptime"))
        try:
            return float(text.split()[0]) if text else None
        except (ValueError, IndexError):
            return None

    def _age_s(self, pid: int, uptime: Optional[float]) -> float:
        """Seconds since the process started (field 22 of /proc/<pid>/stat, in clock ticks)."""
        text = _read_text(os.path.join(self._proc, str(pid), "stat"))
        if not text or uptime is None:
            return 0.0
        try:
            # comm may contain spaces and parentheses: fields resume after the LAST ')'.
            rest = text[text.rindex(")") + 2:].split()
            start_ticks = int(rest[19])  # field 22 overall = index 19 after pid and comm
        except (ValueError, IndexError):
            return 0.0
        return max(0.0, uptime - start_ticks / self._ticks)

    def _descendants(self) -> List[int]:
        """Children, grandchildren, ... of this process (bounded)."""
        found: List[int] = []
        seen: Set[int] = {self._pid}
        frontier = [self._pid]
        while frontier and len(found) < _MAX_CHILDREN_SCANNED:
            pid = frontier.pop()
            task_dir = os.path.join(self._proc, str(pid), "task")
            try:
                tids = os.listdir(task_dir)
            except OSError:
                continue
            for tid in tids:
                text = _read_text(os.path.join(task_dir, tid, "children"))
                for tok in (text or "").split():
                    if tok.isdigit() and int(tok) not in seen:
                        seen.add(int(tok))
                        found.append(int(tok))
                        frontier.append(int(tok))
        return found

    def _child(self, pid: int, uptime: Optional[float]) -> Optional[ChildProcess]:
        status = _read_text(os.path.join(self._proc, str(pid), "status"))
        rss = _status_bytes(status, "VmRSS")
        if rss is None:
            return None
        name = (_read_text(os.path.join(self._proc, str(pid), "comm")) or "?").strip() or "?"
        return ChildProcess(pid=pid, name=name, rss_bytes=rss, age_s=self._age_s(pid, uptime))

    def _tmp_bytes(self) -> Optional[int]:
        """Used bytes on the filesystem holding /tmp — in Cloud Run the writable layer lives in memory."""
        try:
            st = os.statvfs(self._tmp)
        except OSError:
            return None
        return (st.f_blocks - st.f_bfree) * st.f_frsize

    def _fds(self) -> Optional[int]:
        try:
            return len(os.listdir(os.path.join(self._proc, str(self._pid), "fd")))
        except OSError:
            return None

    # -- port --------------------------------------------------------------

    def read(self) -> ProbeReading:
        own = _read_text(os.path.join(self._proc, str(self._pid), "status"))
        uptime = self._uptime_s()
        children = tuple(c for c in (self._child(p, uptime) for p in self._descendants()) if c is not None)
        return ProbeReading(
            rss_bytes=_status_bytes(own, "VmRSS") or 0,
            peak_rss_bytes=_status_bytes(own, "VmHWM"),
            container=self._container(),
            tmp_bytes=self._tmp_bytes(),
            children=children,
            threads=_status_int(own, "Threads"),
            fds=self._fds(),
        )

    def usage_bytes(self) -> Optional[int]:
        v2 = _read_int(os.path.join(self._cg, "memory.current"))
        if v2 is not None:
            return v2
        return _read_int(os.path.join(self._cg, "memory", "memory.usage_in_bytes"))

    def describe(self) -> Dict[str, Any]:
        v2 = os.path.exists(os.path.join(self._cg, "memory.current"))
        v1 = os.path.exists(os.path.join(self._cg, "memory", "memory.usage_in_bytes"))
        reading = self.read()
        c = reading.container
        return {
            "cgroup": "v2" if v2 else "v1" if v1 else "none",
            "container_readable": c is not None,
            "limit_known": bool(c and c.limit_bytes),
            "cache_readable": bool(c and c.cache_bytes is not None),
            "tmp_readable": reading.tmp_bytes is not None,
            "fds_readable": reading.fds is not None,
            "children_readable": os.path.isdir(os.path.join(self._proc, str(self._pid), "task")),
            "uptime_readable": self._uptime_s() is not None,
            "sampled_at": round(time.time(), 1),
        }
