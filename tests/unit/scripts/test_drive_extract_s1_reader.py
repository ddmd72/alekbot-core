"""scripts/infrastructure/drive_extract_s1.py, the reader-shaped run (R3 F4-8, USER_DRIVE_INDEXING_RFC §18 S1, §24.3):
the parent holds MODEL_IN_FLIGHT windows' bytes, their base64 and a request body each — simulated locally, no API
call — while the sandboxed child cuts the next window, so the parent's peak and the cgroup's peak are measured as the
reader runs, not only the child's."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path

import pytest

from src.domain.drive_index_params import MODEL_IN_FLIGHT, MODEL_PAGES_PER_CALL
from tests.unit.adapters.drive_extraction_fixtures import make_pdf

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_extract_s1.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("drive_extract_s1", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**extra):
    return argparse.Namespace(memory_mib=1024, cpu_s=90, wall_s=60, file_mib=64, split=True, reader=True, **extra)


def test_a_simulated_request_holds_the_window_its_base64_and_a_body(script):
    window = b"%PDF-1.7 window bytes"
    held = script.reader_payload(window)
    assert held["data"] == window
    assert base64.b64decode(held["base64"]) == window
    body = json.loads(held["body"])
    assert body["input"][0]["file_data"]["base64"] == held["base64"]
    assert script.held_bytes(held) == len(window) + len(held["base64"]) + len(held["body"])


async def test_reader_run_cuts_each_window_while_the_in_flight_ones_are_held(script, tmp_path):
    pages = MODEL_PAGES_PER_CALL * (MODEL_IN_FLIGHT + 2)
    path = make_pdf(tmp_path / "pages.pdf", [f"page {i}" for i in range(pages)])
    row = await script._measure_one(str(path), "pdf", _args())
    assert row["status"] == "reader" and row["pages_total"] == pages
    assert row["windows"] == MODEL_IN_FLIGHT + 2
    # the first cuts fill the in-flight set; every later cut runs with MODEL_IN_FLIGHT payloads held
    assert row["held_during_cut"] == list(range(MODEL_IN_FLIGHT)) + [MODEL_IN_FLIGHT] * 2
    assert row["in_flight"] == MODEL_IN_FLIGHT
    assert row["held_peak_mib"] > 0
    assert row["host_peak_mib"] > 0 and row["child_peak_mib"] > 0


async def test_reader_run_reports_a_failed_split(script, tmp_path):
    from tests.unit.adapters.drive_extraction_fixtures import encrypt_pdf

    locked = encrypt_pdf(make_pdf(tmp_path / "p.pdf", ["a"]), tmp_path / "l.pdf", user_password="pw")
    row = await script._measure_one(str(locked), "pdf", _args())
    assert row["status"] == "encrypted"


def test_measured_reader_run_passes_the_flag_and_keeps_the_cgroup_peak(script, tmp_path, monkeypatch):
    commands = []
    peaks = iter([100 * 1024 * 1024, 700 * 1024 * 1024])
    monkeypatch.setattr(script, "cgroup_memory_peak_bytes", lambda *a, **k: next(peaks))

    class _Done:
        stdout = json.dumps({"file": "a.pdf", "kind": "pdf", "status": "reader"}) + "\n"
        stderr = ""

    monkeypatch.setattr(script.subprocess, "run", lambda command, **k: commands.append(command) or _Done())
    row = script._run_measured(tmp_path / "a.pdf", "pdf", _args(), split=True)
    assert "--split" in commands[0] and "--reader" in commands[0]
    assert (row["cgroup_peak_before_mib"], row["cgroup_peak_after_mib"]) == (100.0, 700.0)


def test_the_long_pdfs_also_get_a_reader_shaped_run(script, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(script, "_run_measured", lambda path, kind, args, split=False: calls.append(
        (path.name, split, getattr(args, "reader", False))) or {"status": "x"})
    monkeypatch.setattr(script, "kind_for", lambda path: "pdf")
    script.run([tmp_path / "pages_2000.pdf", tmp_path / "tiny.pdf"], argparse.Namespace())
    assert ("pages_2000.pdf", True, True) in calls and ("pages_2000.pdf", True, False) in calls
    assert not any(name == "tiny.pdf" and split for name, split, _ in calls)


def test_main_runs_one_reader_shaped_measurement(script, tmp_path, capsys):
    path = make_pdf(tmp_path / "p.pdf", [f"page {i}" for i in range(12)])
    assert script.main(["--one", str(path), "--kind", "pdf", "--split", "--reader", "--wall-s", "30"]) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert row["status"] == "reader" and row["windows"] == 2
