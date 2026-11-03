"""ParseEngine (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6-§4.9; plan P1): every exit path answers a typed status, the parser's
own status travels as the detail of an ok answer, the child's files are read through the docx engine's discipline and
`pdf_windows` is all or nothing under one wall clock. A stub child (parse_child_stub.py) stands in for the parser except
in the slow tests at the end, which run the real drive child on generated documents."""
import json
import math
import os
import signal
import sys
import time

import pytest

from src.adapters.sandbox_engine import parse_engine
from src.adapters.sandbox_engine.child_launcher import ChildLauncher, ChildResult
from src.adapters.sandbox_engine.parse_engine import ParseEngine
from src.domain.sandbox_contract import SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus
from src.domain.sandbox_windows_frame import split_windows
from tests.unit.adapters.drive_extraction_fixtures import make_docx, make_pdf

# Every test starts a Python child (an import of the drive child's module graph): over 0.5 s with coverage on.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
                       reason="children run as the invoking user: needs a non-root user"),
]

_STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parse_child_stub.py")
_REQUEST_ID = "r" * 16


def _limits(**kw):
    values = dict(wall_s=30.0, memory_bytes=1 << 30, cpu_s=30.0, file_bytes=64 * 1024 * 1024)
    values.update(kw)
    return SandboxLimits(**values)


def _meta(limits=None, **kw):
    return SandboxMeta(request_id=_REQUEST_ID, limits=limits or _limits(), **kw)


class Rig:
    """One request: a workdir with the body file in it, an output root, and the engine for one op."""

    def __init__(self, tmp_path, op, body, *, script=_STUB, **engine_kw):
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        self.out_root = tmp_path / "out"
        self.out_root.mkdir(mode=0o700)
        self.body = self.workdir / "body"
        self.body.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.engine = ParseEngine(ChildLauncher(nproc=4000, nofile=256), op=op, output_root=str(self.out_root),
                                  child_script=script, **engine_kw)

    async def run(self, limits=None, **meta_kw):
        limits = limits or _limits()
        return await self.engine.run(str(self.body), str(self.workdir), limits, _meta(limits, **meta_kw))

    def output(self, result) -> bytes:
        with open(result.output_path, "rb") as fh:
            return fh.read()

    def out_dirs(self):
        return os.listdir(self.out_root)

    def request(self, index=0):
        return json.loads((self.workdir / f"request-{index}.json").read_text())


def extract_rig(tmp_path, spec="ok", **kw):
    return Rig(tmp_path, SandboxOp.PARSE_EXTRACT, spec if isinstance(spec, dict) else {"*": spec}, **kw)


def count_rig(tmp_path, spec="ok", **kw):
    return Rig(tmp_path, SandboxOp.PARSE_PDF_PAGE_COUNT, spec if isinstance(spec, dict) else {"*": spec}, **kw)


def windows_rig(tmp_path, spec="ok", **kw):
    return Rig(tmp_path, SandboxOp.PARSE_PDF_WINDOWS, spec if isinstance(spec, dict) else {"*": spec}, **kw)


def test_a_non_parse_op_is_refused(tmp_path):
    with pytest.raises(ValueError):
        ParseEngine(ChildLauncher(nproc=4000), op=SandboxOp.RENDER_DOCX, output_root=str(tmp_path))


# -- extract ----------------------------------------------------------------------------------------------------------

async def test_extract_ok_carries_the_parser_status_as_detail_and_the_result_as_body(tmp_path):
    rig = extract_rig(tmp_path)
    result = await rig.run(kind="docx", ext="docx")
    assert (result.status, result.detail) == (SandboxStatus.OK, "text")
    body = json.loads(rig.output(result))
    assert body["status"] == "text" and body["pages_total"] == 1
    assert os.path.dirname(os.path.dirname(result.output_path)) == str(rig.out_root)   # a copy under output_root


async def test_extract_stages_the_body_as_a_hard_link_with_the_extension(tmp_path):
    rig = extract_rig(tmp_path)
    await rig.run(kind="docx", ext="docx")
    request = rig.request()
    source = rig.workdir / "source.docx"
    assert request["path"] == str(source) and request["kind"] == "docx" and request["op"] == "extract"
    assert os.stat(source).st_ino == os.stat(rig.body).st_ino


async def test_extract_without_an_extension_links_plain_source(tmp_path):
    rig = extract_rig(tmp_path)
    await rig.run(kind="txt")
    assert rig.request()["path"] == str(rig.workdir / "source")


async def test_the_childs_request_carries_the_launcher_limits_and_no_nproc(tmp_path):
    rig = extract_rig(tmp_path)
    limits = _limits(cpu_s=12.2, memory_bytes=900 << 20, file_bytes=5 << 20)
    await rig.run(limits, kind="txt")
    request = rig.request()
    assert request["cpu_s"] == math.ceil(12.2) == 13
    assert (request["memory_bytes"], request["file_bytes"]) == (900 << 20, 5 << 20)
    assert "nproc" not in request
    assert request["out"] == str(rig.workdir / "result-0.json")


async def test_the_child_reads_its_request_from_stdin_and_runs_under_the_fixed_environment(tmp_path):
    rig = extract_rig(tmp_path)
    result = await rig.run(kind="txt")
    echoed = json.loads(json.loads(rig.output(result))["text"])
    assert echoed["op"] == "extract" and echoed["kind"] == "txt"


@pytest.mark.parametrize("status", ["no_text", "unsupported", "too_large", "encrypted", "error"])
async def test_each_parser_status_is_an_ok_answer_with_that_status_as_detail(tmp_path, status):
    rig = extract_rig(tmp_path, f"status:{status}")
    result = await rig.run(kind="pdf", ext="pdf")
    assert (result.status, result.detail) == (SandboxStatus.OK, status)
    body = json.loads(rig.output(result))
    assert body["status"] == status and len(body["detail"]) == 300   # cut at 300


async def test_no_text_keeps_pages_total_and_capture_signals_in_the_body(tmp_path):
    rig = extract_rig(tmp_path, "no_text")
    result = await rig.run(kind="pdf", ext="pdf")
    assert result.detail == "no_text"
    body = json.loads(rig.output(result))
    assert body["pages_total"] == 5 and body["capture_signals"] == {"pages_sampled": 2}


async def test_extract_without_a_kind_is_bad_request_and_starts_no_child(tmp_path):
    rig = extract_rig(tmp_path)
    result = await rig.run(ext="docx")
    assert result.status is SandboxStatus.BAD_REQUEST and result.output_path is None
    assert not (rig.workdir / "request-0.json").exists()


async def test_pdf_windows_without_windows_is_bad_request(tmp_path):
    rig = windows_rig(tmp_path)
    result = await rig.run()
    assert result.status is SandboxStatus.BAD_REQUEST and result.output_path is None


# -- exit paths -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("behaviour, status, detail", [
    ("memory_error", SandboxStatus.MEMORY, "MemoryError"),
    ("sigkill", SandboxStatus.MEMORY, "killed"),
    ("exit7", SandboxStatus.ERROR, "exit 7"),
    ("exit4", SandboxStatus.ERROR, "child refused the request"),
    ("no_result", SandboxStatus.ERROR, "no valid result"),
    ("garbage", SandboxStatus.ERROR, "no valid result"),
    ("extra_key", SandboxStatus.ERROR, "no valid result"),
    ("bad_status", SandboxStatus.ERROR, "no valid result"),
    ("result_symlink", SandboxStatus.ERROR, "no valid result"),
    ("result_fifo", SandboxStatus.ERROR, "no valid result"),
    ("result_hardlink", SandboxStatus.ERROR, "no valid result"),
    ("result_oversized", SandboxStatus.ERROR, "no valid result"),
])
async def test_every_exit_path_answers_its_status_and_no_output(tmp_path, behaviour, status, detail):
    rig = extract_rig(tmp_path, behaviour)
    result = await rig.run(kind="txt")
    assert (result.status, result.detail, result.output_path) == (status, detail, None)
    assert rig.out_dirs() == []


async def test_wall_clock_is_a_timeout(tmp_path):
    rig = extract_rig(tmp_path, "sleep")
    result = await rig.run(_limits(wall_s=1.5), kind="txt")
    assert result.status is SandboxStatus.TIMEOUT and result.output_path is None


async def test_cpu_limit_is_a_timeout(tmp_path):
    rig = extract_rig(tmp_path, "spin")
    result = await rig.run(_limits(cpu_s=1.0, wall_s=30.0), kind="txt")
    assert result.status is SandboxStatus.TIMEOUT and "cpu limit 1s" in result.detail


@pytest.mark.parametrize("code, status", [
    (-signal.SIGXCPU, SandboxStatus.TIMEOUT), (-signal.SIGKILL, SandboxStatus.MEMORY),
    (parse_engine.EXIT_MEMORY, SandboxStatus.MEMORY), (parse_engine.EXIT_BAD_REQUEST, SandboxStatus.ERROR),
    (5, SandboxStatus.ERROR), (-signal.SIGSEGV, SandboxStatus.ERROR),
])
async def test_exit_map_matches_the_in_process_adapter(tmp_path, code, status):
    class Launcher:
        async def run(self, *args, **kw):
            return ChildResult(returncode=code, timed_out=False, peak_rss_bytes=7, stderr_length=0, stderr_sha256="")

    rig = extract_rig(tmp_path)
    rig.engine._launcher = Launcher()
    result = await rig.run(kind="txt")
    assert result.status is status and result.peak_rss_bytes == 7


async def test_a_request_file_that_cannot_be_staged_is_an_error(tmp_path):
    rig = extract_rig(tmp_path)
    os.symlink("/etc/hosts", rig.workdir / "request-0.json")
    result = await rig.run(kind="txt")
    assert (result.status, result.detail) == (SandboxStatus.ERROR, "request could not be staged")


async def test_a_response_over_the_cap_is_too_large(tmp_path):
    rig = extract_rig(tmp_path, response_cap_bytes=50)
    result = await rig.run(kind="txt")
    assert result.status is SandboxStatus.TOO_LARGE and result.output_path is None and rig.out_dirs() == []


async def test_failures_log_one_warning_without_the_kind_or_the_window_plan(tmp_path, caplog):
    rig = extract_rig(tmp_path, "exit7")
    with caplog.at_level("WARNING"):
        await rig.run(kind="secretkind", ext="secretext")
    lines = [r.getMessage() for r in caplog.records if "parse op=" in r.getMessage()]
    assert len(lines) == 1 and "parse.extract" in lines[0] and "exit 7" in lines[0]
    assert "secretkind" not in lines[0] and "secretext" not in lines[0]


# -- page count -------------------------------------------------------------------------------------------------------

async def test_page_count_ok_reads_the_body_in_place(tmp_path):
    rig = count_rig(tmp_path)
    result = await rig.run()
    assert (result.status, result.detail) == (SandboxStatus.OK, "text")
    assert json.loads(rig.output(result)) == {"status": "text", "pages_total": 3}
    assert rig.request()["path"] == str(rig.body) and rig.request()["op"] == "pdf_page_count"


async def test_page_count_encrypted_is_ok_with_the_status_as_detail(tmp_path):
    rig = count_rig(tmp_path, "status:encrypted")
    result = await rig.run()
    assert (result.status, result.detail) == (SandboxStatus.OK, "encrypted")


async def test_page_count_refuses_a_status_outside_its_set(tmp_path):
    rig = count_rig(tmp_path, "status:no_text")
    result = await rig.run()
    assert (result.status, result.detail) == (SandboxStatus.ERROR, "no valid result")


# -- windows ----------------------------------------------------------------------------------------------------------

async def test_windows_round_trip_in_plan_order(tmp_path):
    rig = windows_rig(tmp_path)
    result = await rig.run(windows=((0, 2), (2, 5), (10, 1)))
    assert (result.status, result.detail) == (SandboxStatus.OK, "text")
    assert split_windows(rig.output(result)) == [b"%PDF-window-0", b"%PDF-window-1", b"%PDF-window-2"]
    first, third = rig.request(0), rig.request(2)
    assert (first["first_page"], first["page_count"], first["op"]) == (0, 2, "cut_pdf_window")
    assert (third["first_page"], third["page_count"]) == (10, 1)
    assert first["path"] == str(rig.body) and first["out_path"] == str(rig.workdir / "window-0.pdf")


async def test_one_window_is_a_batch_of_one(tmp_path):
    rig = windows_rig(tmp_path)
    result = await rig.run(windows=((4, 3),))
    assert split_windows(rig.output(result)) == [b"%PDF-window-0"]


async def test_the_second_window_failing_leaves_no_output_dir(tmp_path):
    rig = windows_rig(tmp_path, {"1": "exit7"})
    result = await rig.run(windows=((0, 1), (1, 1), (2, 1)))
    assert (result.status, result.detail, result.output_path) == (SandboxStatus.ERROR, "exit 7", None)
    assert rig.out_dirs() == []
    assert not (rig.workdir / "request-2.json").exists()   # no child after the failure


async def test_a_window_with_a_parser_status_answers_ok_with_a_status_body_and_no_frame(tmp_path):
    rig = windows_rig(tmp_path, {"1": "status:encrypted"})
    result = await rig.run(windows=((0, 1), (1, 1)))
    assert (result.status, result.detail) == (SandboxStatus.OK, "encrypted")
    body = json.loads(rig.output(result))
    assert set(body) == {"status", "detail"} and body["status"] == "encrypted"


@pytest.mark.parametrize("behaviour", ["window_symlink", "window_fifo", "window_empty", "window_missing", "window_hardlink"])
async def test_a_window_file_that_is_not_a_plain_file_is_refused(tmp_path, behaviour):
    rig = windows_rig(tmp_path, {"1": behaviour})
    result = await rig.run(windows=((0, 1), (1, 1)))
    assert (result.status, result.detail, result.output_path) == (SandboxStatus.ERROR, "no valid result", None)
    assert rig.out_dirs() == []


async def test_windows_over_the_cap_are_too_large_and_leave_nothing(tmp_path):
    rig = windows_rig(tmp_path, "window_big", response_cap_bytes=4096)
    result = await rig.run(windows=((0, 1), (1, 1)))
    assert result.status is SandboxStatus.TOO_LARGE and result.output_path is None and rig.out_dirs() == []


async def test_the_wall_used_up_between_windows_is_a_timeout(tmp_path):
    class SlowLauncher:
        def __init__(self, inner):
            self.inner, self.calls = inner, 0

        async def run(self, *args, **kw):
            self.calls += 1
            result = await self.inner.run(*args, **kw)
            time.sleep(1.5)   # the first window eats the whole budget
            return result

    rig = windows_rig(tmp_path)
    rig.engine._launcher = SlowLauncher(rig.engine._launcher)
    result = await rig.run(_limits(wall_s=1.0), windows=((0, 1), (1, 1), (2, 1)))
    assert result.status is SandboxStatus.TIMEOUT and result.output_path is None and rig.out_dirs() == []


async def test_each_child_gets_only_the_time_left(tmp_path):
    seen = []

    class Recording:
        def __init__(self, inner):
            self.inner = inner

        async def run(self, *args, limits, **kw):
            seen.append(limits.wall_s)
            time.sleep(0.2)
            return await self.inner.run(*args, limits=limits, **kw)

    rig = windows_rig(tmp_path)
    rig.engine._launcher = Recording(rig.engine._launcher)
    await rig.run(_limits(wall_s=20.0), windows=((0, 1), (1, 1)))
    assert seen[0] < 20.0 and seen[1] < seen[0]


async def test_cancellation_leaves_no_output_dir(tmp_path):
    import asyncio

    rig = windows_rig(tmp_path, {"1": "sleep"})
    task = asyncio.ensure_future(rig.run(windows=((0, 1), (1, 1))))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.out_dirs() == []


# -- the real drive child ---------------------------------------------------------------------------------------------

def _real_rig(tmp_path, op, source):
    rig = Rig(tmp_path, op, b"", script=parse_engine._CHILD_SCRIPT)
    rig.body.write_bytes(source.read_bytes())
    return rig


async def test_real_child_extracts_a_text_docx(tmp_path):
    source = make_docx(tmp_path / "plan.docx", ["Weekend in Coimbra, March 6-8", "Gift for Clara: a cookbook"])
    rig = _real_rig(tmp_path, SandboxOp.PARSE_EXTRACT, source)
    result = await rig.run(kind="docx", ext="docx")
    assert (result.status, result.detail) == (SandboxStatus.OK, "text")
    body = json.loads(rig.output(result))
    assert "Weekend in Coimbra" in body["text"] and "cookbook" in body["text"]


async def test_real_child_counts_and_cuts_a_three_page_pdf(tmp_path):
    source = make_pdf(tmp_path / "trip.pdf", ["Page one", "Page two", "Page three"])
    (tmp_path / "a").mkdir()
    counter = _real_rig(tmp_path / "a", SandboxOp.PARSE_PDF_PAGE_COUNT, source)
    counted = await counter.run()
    assert json.loads(counter.output(counted)) == {"status": "text", "pages_total": 3}
    (tmp_path / "b").mkdir()
    rig = _real_rig(tmp_path / "b", SandboxOp.PARSE_PDF_WINDOWS, source)
    result = await rig.run(windows=((0, 2), (2, 1)))
    assert (result.status, result.detail) == (SandboxStatus.OK, "text")
    windows = split_windows(rig.output(result))
    assert len(windows) == 2 and all(w.startswith(b"%PDF") for w in windows)
