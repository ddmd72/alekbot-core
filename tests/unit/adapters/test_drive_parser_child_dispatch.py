"""The drive parser's child dispatching to the readers (USER_DRIVE_INDEXING_RFC §9, §24.3): `extract` and the lazy
page-window ops `pdf_page_count` / `cut_pdf_window` end to end through a real child process, the result in a file,
nothing on stdout."""
import json
import subprocess
import sys
from pathlib import Path

from tests.unit.adapters.drive_extraction_fixtures import make_pdf

_ROOT = Path(__file__).resolve().parents[3]


def _run(stdin: bytes):
    return subprocess.run([sys.executable, "-B", "-E", "-s", "-m", "src.adapters.drive_parser.child"],
                          input=stdin, cwd=_ROOT, capture_output=True, timeout=60)


def _request(tmp_path, **fields):
    base = {"out": str(tmp_path / "result.json"), "memory_bytes": 2 * 1024 ** 3, "cpu_s": 30,
            "file_bytes": 64 * 1024 ** 2}
    return json.dumps({**base, **fields}).encode()


def test_a_text_file_end_to_end(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("hello from the child", encoding="utf-8")
    proc = _run(_request(tmp_path, op="extract", path=str(source), kind="text"))
    assert proc.returncode == 0, proc.stderr
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result == {"status": "text", "text": "hello from the child", "pages_total": 0, "truncated": False,
                      "detail": ""}
    assert not (tmp_path / "result.json.part").exists()


def test_a_parser_that_prints_cannot_corrupt_the_result(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("x", encoding="utf-8")
    proc = _run(_request(tmp_path, op="extract", path=str(source), kind="text"))
    assert proc.stdout == b""                                 # nothing on stdout; the result is a file
    assert json.loads((tmp_path / "result.json").read_text())["text"] == "x"


def test_split_op_end_to_end(tmp_path):
    source = make_pdf(tmp_path / "s.pdf", ["a", "b", "c"])
    windows = tmp_path / "w"
    windows.mkdir()
    proc = _run(_request(tmp_path, op="pdf_page_count", path=str(source)))
    assert proc.returncode == 0, proc.stderr
    assert json.loads((tmp_path / "result.json").read_text()) == {"status": "text", "pages_total": 3}
    for first, name, pages in ((0, "window-0000.pdf", 2), (2, "window-0001.pdf", 1)):
        proc = _run(_request(tmp_path, op="cut_pdf_window", path=str(source), first_page=first, page_count=2,
                             out_path=str(windows / name)))
        assert proc.returncode == 0, proc.stderr
        assert json.loads((tmp_path / "result.json").read_text()) == {"status": "text", "pages": pages}
    assert sorted(p.name for p in windows.iterdir()) == ["window-0000.pdf", "window-0001.pdf"]
