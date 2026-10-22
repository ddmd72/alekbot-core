"""scripts/infrastructure/drive_extract_s1.py (USER_DRIVE_INDEXING_RFC §18 S1): generates the bomb set, derives each
file's kind as the tick will, and measures one sandboxed extraction per fresh process."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_extract_s1.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("drive_extract_s1", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.slow
def test_generated_set_gets_the_kinds_the_tick_would_derive(script, tmp_path):
    paths = {p.name: p for p in script.generate(tmp_path, scale=0.001)}
    kinds = {name: script.kind_for(path) for name, path in paths.items()}
    assert kinds["phase0_member_bomb.docx"] == "docx"
    assert kinds["phase0_inflation_bomb.docx"] == "docx"
    assert kinds["declared_16384x1048576.xlsx"] == "xlsx"
    assert kinds["hostile_repeats.ods"] == "ods"
    assert kinds["pages_2000.pdf"] == "pdf" and kinds["encrypted.pdf"] == "pdf"
    assert kinds["download_45mb.txt"] == "text" and kinds["download_45mb.csv"] == "csv"
    assert kinds["nested.html"] == "html" and kinds["long.docx"] == "docx"


@pytest.mark.datafile
def test_rlimit_probe_reports_a_verdict(script):
    probe = script.rlimit_probe(limit_mib=64)
    assert isinstance(probe["rlimit_as_honoured"], bool) and probe["platform"]


@pytest.mark.datafile
def test_one_measured_run_prints_a_json_row(script, tmp_path, capsys):
    path = tmp_path / "tiny.txt"
    path.write_text("hello", encoding="utf-8")
    assert script.main(["--one", str(path), "--kind", "text", "--wall-s", "30"]) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert row["status"] == "text" and row["text_chars"] == 5
    assert row["child_peak_mib"] > 0 and "seconds" in row


def test_summary_names_the_limits_and_every_file(script):
    args = argparse.Namespace(memory_mib=1024, cpu_s=90, wall_s=120, file_mib=64)
    rows = [{"file": "a.pdf", "kind": "pdf", "status": "memory", "seconds": 3.1, "child_peak_mib": 1010.0,
             "host_peak_mib": 60.0}]
    text = script.summary(rows, {"platform": "linux", "rlimit_as_honoured": True}, args)
    assert "RLIMIT_AS honoured=True" in text and "AS 1024 MiB" in text
    assert "a.pdf" in text and "memory" in text


def test_address_space_floor_bisects_to_the_smallest_limit_that_reads(script, tmp_path, monkeypatch):
    calls = []

    def fake_run(path, kind, args, split=False):
        calls.append(args.memory_mib)
        return {"status": "text" if args.memory_mib >= 700 else "memory"}

    monkeypatch.setattr(script, "_run_measured", fake_run)
    args = argparse.Namespace(memory_mib=1024, cpu_s=90, wall_s=120, file_mib=64)
    result = script.address_space_floor(tmp_path / "tiny.docx", "docx", args)
    assert 700 <= result["floor_mib"] <= 732
    assert calls[0] == 4096 and args.memory_mib == 1024           # the caller's limits are untouched


def test_address_space_floor_when_even_the_top_fails(script, tmp_path, monkeypatch):
    monkeypatch.setattr(script, "_run_measured", lambda path, kind, args, split=False: {"status": "memory"})
    args = argparse.Namespace(memory_mib=1024, cpu_s=90, wall_s=120, file_mib=64)
    assert script.address_space_floor(tmp_path / "tiny.pdf", "pdf", args)["floor_mib"] is None


# =============================================================================
# Review A (m): the cgroup's memory peak around each run, and a PDF whose pages share one resource
# =============================================================================

def test_cgroup_peak_reads_cgroup_v2_memory_peak(script, tmp_path):
    (tmp_path / "memory.peak").write_text("1073741824\n")
    assert script.cgroup_memory_peak_bytes(tmp_path) == 1073741824


def test_cgroup_peak_falls_back_to_cgroup_v1(script, tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "memory.max_usage_in_bytes").write_text("2097152\n")
    assert script.cgroup_memory_peak_bytes(tmp_path) == 2097152


def test_cgroup_peak_is_none_without_a_cgroup(script, tmp_path):
    assert script.cgroup_memory_peak_bytes(tmp_path / "absent") is None


def test_each_measured_run_records_the_cgroup_peak_before_and_after(script, tmp_path, monkeypatch):
    peaks = iter([300 * 1024 * 1024, 900 * 1024 * 1024])
    monkeypatch.setattr(script, "cgroup_memory_peak_bytes", lambda *a, **k: next(peaks))

    class _Done:
        stdout = json.dumps({"file": "a.pdf", "kind": "pdf", "status": "split"}) + "\n"
        stderr = ""

    monkeypatch.setattr(script.subprocess, "run", lambda *a, **k: _Done())
    args = argparse.Namespace(memory_mib=1024, cpu_s=90, wall_s=120, file_mib=64)
    row = script._run_measured(tmp_path / "a.pdf", "pdf", args, split=True)
    assert row["cgroup_peak_before_mib"] == 300.0 and row["cgroup_peak_after_mib"] == 900.0
    text = script.summary([row], {"platform": "linux", "rlimit_as_honoured": True}, args)
    assert "900.0" in text


@pytest.mark.slow
def test_generated_set_has_a_pdf_whose_pages_share_one_image(script, tmp_path):
    from pypdf import PdfReader

    paths = {p.name: p for p in script.generate(tmp_path, scale=0.01)}
    path = paths["shared_image_pages.pdf"]
    assert script.kind_for(path) == "pdf"
    reader = PdfReader(str(path))
    assert len(reader.pages) >= 2
    images = {page["/Resources"]["/XObject"].raw_get("/Im0").idnum for page in reader.pages}
    assert len(images) == 1                                      # every page draws the same stream


def test_the_shared_resource_pdf_is_also_split(script, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(script, "_run_measured",
                        lambda path, kind, args, split=False: calls.append((path.name, split)) or {"status": "x"})
    monkeypatch.setattr(script, "kind_for", lambda path: "pdf")
    script.run([tmp_path / "shared_image_pages.pdf"], argparse.Namespace())
    assert ("shared_image_pages.pdf", True) in calls
