"""ParseEngine's file helpers and answer path, driven directly (no child process): every refusal answers a typed
failure with a WARNING/ERROR log and leaves no half-written output directory behind."""
import logging
import os

import pytest

from src.adapters.sandbox_engine import parse_engine
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.parse_engine import ParseEngine, _Failure
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_windows_frame import frame_prefix, window_prefix

CAP = 1 << 20


def _engine(root, op=SandboxOp.PARSE_EXTRACT, **kw):
    return ParseEngine(ChildLauncher(nproc=4000), op=op, output_root=str(root), **kw)


def _root(tmp_path):
    root = tmp_path / "out"
    root.mkdir(mode=0o700)
    return root


def test_no_output_root_creates_a_private_directory():
    engine = ParseEngine(ChildLauncher(nproc=4000), op=SandboxOp.PARSE_EXTRACT)
    root = engine._output_root
    try:
        assert os.path.basename(root).startswith("sandbox_out_") and os.stat(root).st_mode & 0o077 == 0
    finally:
        os.rmdir(root)


def test_a_group_writable_output_root_is_refused_with_an_error_log(tmp_path, caplog):
    root = tmp_path / "open"
    root.mkdir()
    os.chmod(root, 0o770)
    with caplog.at_level(logging.INFO), pytest.raises(ValueError):
        _engine(root)
    assert any(r.levelno == logging.ERROR and "output root" in r.getMessage() for r in caplog.records)


async def test_a_result_outside_the_closed_schema_answers_no_valid_result(tmp_path):
    engine = _engine(_root(tmp_path))
    with pytest.raises(_Failure) as raised:
        await engine._answer({"status": "text", "surprise": 1})
    assert (raised.value.status, raised.value.detail) == (SandboxStatus.ERROR, "no valid result")


async def test_an_unwritable_output_root_answers_error_with_a_warning(tmp_path, caplog):
    root = _root(tmp_path)
    engine = _engine(root)
    root.rmdir()   # the output root vanished after start
    with caplog.at_level(logging.INFO), pytest.raises(_Failure) as raised:
        await engine._answer({"status": "text", "text": "Rent is due.", "pages_total": 1})
    assert (raised.value.status, raised.value.detail) == (SandboxStatus.ERROR, "output could not be written")
    assert any(r.levelno == logging.WARNING and "could not be written" in r.getMessage() for r in caplog.records)


def test_a_failed_write_removes_its_output_directory(tmp_path, monkeypatch):
    root = _root(tmp_path)

    def refuse(_path):
        raise OSError("disk full")

    monkeypatch.setattr(parse_engine, "_create_private", refuse)
    with pytest.raises(OSError):
        parse_engine._write_output(b"{}", str(root))
    assert os.listdir(root) == []


def test_a_missing_body_cannot_be_staged(tmp_path, caplog):
    work = tmp_path / "work"
    work.mkdir()
    with caplog.at_level(logging.INFO), pytest.raises(_Failure) as raised:
        parse_engine._link_source(str(tmp_path / "absent"), str(work), "pdf")
    assert (raised.value.status, raised.value.detail) == (SandboxStatus.ERROR, "source could not be staged")
    assert any(r.levelno == logging.WARNING and "could not be linked" in r.getMessage() for r in caplog.records)


def test_a_missing_workdir_reads_no_result_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        assert parse_engine._read_result(str(tmp_path / "gone"), "result.json") is None
    assert any(r.levelno == logging.WARNING and "workdir refused" in r.getMessage() for r in caplog.records)


def _frame(tmp_path, names, files, cap=CAP):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    for name, data in files.items():
        (work / name).write_bytes(data)
    return parse_engine._write_frame(str(work), names, str(_root(tmp_path)), cap)


def test_a_frame_streams_the_windows_in_order(tmp_path):
    path = _frame(tmp_path, ["w0", "w1"], {"w0": b"AAA", "w1": b"BB"})
    expected = frame_prefix(2) + window_prefix(3) + b"AAA" + window_prefix(2) + b"BB"
    with open(path, "rb") as fh:
        assert fh.read() == expected


def test_an_empty_window_fails_the_frame_and_leaves_no_output(tmp_path):
    with pytest.raises(_Failure) as raised:
        _frame(tmp_path, ["w0"], {"w0": b""})
    assert (raised.value.status, raised.value.detail) == (SandboxStatus.ERROR, "no valid result")
    assert os.listdir(tmp_path / "out") == []


def test_a_frame_over_the_cap_is_too_large_and_leaves_no_output(tmp_path):
    with pytest.raises(_Failure) as raised:
        _frame(tmp_path, ["w0"], {"w0": b"A" * 100}, cap=50)
    assert raised.value.status is SandboxStatus.TOO_LARGE
    assert os.listdir(tmp_path / "out") == []


def test_a_window_that_shrank_after_the_check_fails_the_frame(tmp_path, monkeypatch):
    real = parse_engine._open_regular
    monkeypatch.setattr(parse_engine, "_open_regular", lambda name, fd: (lambda r: (r[0], r[1] + 4))(real(name, fd)))
    with pytest.raises(_Failure) as raised:
        _frame(tmp_path, ["w0"], {"w0": b"AAAA"})
    assert (raised.value.status, raised.value.detail) == (SandboxStatus.ERROR, "no valid result")
    assert os.listdir(tmp_path / "out") == []


def test_a_window_file_that_vanished_fails_the_frame_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.INFO), pytest.raises(_Failure) as raised:
        _frame(tmp_path, ["w0"], {})
    assert (raised.value.status, raised.value.detail) == (SandboxStatus.ERROR, "no valid result")
    assert any(r.levelno == logging.WARNING and "window file refused" in r.getMessage() for r in caplog.records)
    assert os.listdir(tmp_path / "out") == []
