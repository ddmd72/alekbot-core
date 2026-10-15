"""ProcMemoryProbe against a fake /proc and cgroup tree (layouts probed on Cloud Run 2026-10-15)."""
from pathlib import Path

from src.adapters.proc_memory_probe import ProcMemoryProbe
from src.domain.memory_telemetry import MIB

SELF = 100


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _proc(root: Path, pid: int, rss_kb: int, comm: str, children=(), hwm_kb=None, start_ticks=0, threads=4):
    d = root / str(pid)
    _w(d / "status", f"Name:\t{comm}\nVmHWM:\t{hwm_kb or rss_kb} kB\nVmRSS:\t{rss_kb} kB\nThreads:\t{threads}\n")
    _w(d / "comm", comm + "\n")
    # fields: pid (comm) state ppid ... — starttime is field 22
    after = ["S"] + ["0"] * 18 + [str(start_ticks)] + ["0"] * 5
    _w(d / "stat", f"{pid} ({comm}) " + " ".join(after) + "\n")
    _w(d / "task" / str(pid) / "children", " ".join(str(c) for c in children))
    (d / "fd").mkdir(exist_ok=True)
    for i in range(3):
        (d / "fd" / str(i)).write_text("")


def _tree(tmp_path: Path):
    proc, cg = tmp_path / "proc", tmp_path / "cg"
    _w(proc / "uptime", "1000.00 900.00\n")
    return proc, cg


def test_cgroup_v1_layout_as_probed_on_cloud_run(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=300 * 1024, comm="python", hwm_kb=320 * 1024)
    _w(cg / "memory" / "memory.usage_in_bytes", str(646 * MIB))
    _w(cg / "memory" / "memory.limit_in_bytes", str(1024 * MIB))
    _w(cg / "memory" / "memory.stat", "cache 315000000\nrss 320000000\n")

    r = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF).read()

    assert r.rss_bytes == 300 * MIB and r.peak_rss_bytes == 320 * MIB
    assert r.container is not None
    assert r.container.usage_bytes == 646 * MIB and r.container.limit_bytes == 1024 * MIB
    assert r.container.cache_bytes == 315000000
    assert r.threads == 4 and r.fds == 3


def test_cgroup_v1_unlimited_becomes_unknown_limit(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=1024, comm="python")
    _w(cg / "memory" / "memory.usage_in_bytes", str(10 * MIB))
    _w(cg / "memory" / "memory.limit_in_bytes", "9223372036854771712")

    r = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF).read()

    assert r.container is not None and r.container.limit_bytes is None


def test_cgroup_v2_preferred_and_max_means_no_limit(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=1024, comm="python")
    _w(cg / "memory.current", str(200 * MIB))
    _w(cg / "memory.max", "max\n")
    _w(cg / "memory.stat", "anon 100\nfile 5000\n")
    _w(cg / "memory" / "memory.usage_in_bytes", str(999 * MIB))  # must be ignored when v2 exists

    r = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF).read()

    assert r.container is not None
    assert r.container.usage_bytes == 200 * MIB and r.container.limit_bytes is None
    assert r.container.cache_bytes == 5000


def test_no_cgroup_files_means_unknown_container_not_an_error(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=2048, comm="python")

    probe = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF)
    r = probe.read()

    assert r.container is None and r.rss_bytes == 2 * MIB
    assert probe.describe()["cgroup"] == "none"
    assert probe.describe()["container_readable"] is False


def test_children_are_found_recursively_with_rss_and_age(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=1024, comm="python", children=(200,))
    _proc(proc, 200, rss_kb=50 * 1024, comm="node", children=(300,), start_ticks=60000)   # started 600 s in
    _proc(proc, 300, rss_kb=120 * 1024, comm="chrome", start_ticks=99000)                 # started 990 s in

    r = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF).read()

    by_name = {c.name: c for c in r.children}
    assert set(by_name) == {"node", "chrome"}
    assert by_name["chrome"].rss_bytes == 120 * MIB
    assert round(by_name["node"].age_s) == 400 and round(by_name["chrome"].age_s) == 10


def test_a_vanished_child_is_skipped(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=1024, comm="python", children=(777,))  # 777 has no /proc entry

    r = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF).read()

    assert r.children == ()


def test_comm_with_spaces_and_parentheses_does_not_shift_start_time(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=1024, comm="python", children=(400,))
    _proc(proc, 400, rss_kb=1024, comm="Web Content (x)", start_ticks=90000)

    r = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF).read()

    assert len(r.children) == 1 and round(r.children[0].age_s) == 100


def test_tmp_bytes_is_used_space_of_the_filesystem(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=1024, comm="python")

    r = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF).read()

    assert r.tmp_bytes is not None and r.tmp_bytes > 0


def test_usage_bytes_reads_only_the_container_figure(tmp_path):
    proc, cg = _tree(tmp_path)
    _proc(proc, SELF, rss_kb=1024, comm="python")
    probe = ProcMemoryProbe(str(proc), str(cg), str(tmp_path), pid=SELF)
    assert probe.usage_bytes() is None
    _w(cg / "memory" / "memory.usage_in_bytes", str(77 * MIB))
    assert probe.usage_bytes() == 77 * MIB
    _w(cg / "memory.current", str(5 * MIB))
    assert probe.usage_bytes() == 5 * MIB
