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


def test_rlimit_probe_reports_a_verdict(script):
    probe = script.rlimit_probe(limit_mib=64)
    assert isinstance(probe["rlimit_as_honoured"], bool) and probe["platform"]


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
