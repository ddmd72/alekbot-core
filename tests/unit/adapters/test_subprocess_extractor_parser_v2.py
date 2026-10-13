"""Parser@2 across the sandbox boundary (USER_DRIVE_INDEXING_RFC §25.3, §25.4.8): the version is 2, the child reports
`script_rendered` only when set, and the parent passes it on a `text` outcome after checking its type."""
import json
import subprocess
import sys
from pathlib import Path

from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.drive_extraction import PARSER, ExtractionOutcome, ExtractionRequest, ExtractionStatus

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PAGE = ("<html lang='en'><head><title>Planner</title><script>" + "var x=1;" * 5000 + "</script></head>"
                "<body><div id='root'></div></body></html>")


def test_parser_version_is_2():
    assert SubprocessContentExtractor.VERSION == 2
    assert SubprocessContentExtractor().stamp == f"{PARSER}@2"


def test_outcome_has_the_flag_off_by_default():
    assert ExtractionOutcome(status=ExtractionStatus.TEXT, text="x").script_rendered is False


def _child(tmp_path, source: Path, kind: str) -> dict:
    request = {"out": str(tmp_path / "result.json"), "memory_bytes": 2 * 1024 ** 3, "cpu_s": 30,
               "file_bytes": 64 * 1024 ** 2, "op": "extract", "path": str(source), "kind": kind}
    proc = subprocess.run([sys.executable, "-B", "-E", "-s", "-m", "src.adapters.drive_parser.child"],
                          input=json.dumps(request).encode(), cwd=_ROOT, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))


def test_the_child_reports_a_script_rendered_page(tmp_path):
    source = tmp_path / "app.html"
    source.write_text(_SCRIPT_PAGE, encoding="utf-8")
    result = _child(tmp_path, source, "html")
    assert result["status"] == "text" and result["script_rendered"] is True
    assert result["text"].startswith("Title: Planner")


def test_the_child_omits_the_flag_when_unset(tmp_path):
    source = tmp_path / "a.html"
    source.write_text("<html><body><p>Ordinary page text.</p></body></html>", encoding="utf-8")
    assert "script_rendered" not in _child(tmp_path, source, "html")


def test_the_parent_passes_the_flag_on_a_text_outcome():
    extractor = SubprocessContentExtractor()
    outcome = extractor._from_result({"status": "text", "text": "Title: Planner", "pages_total": 0,
                                      "truncated": False, "script_rendered": True})
    assert outcome.status is ExtractionStatus.TEXT and outcome.script_rendered is True
    assert outcome.extracted_by == f"{PARSER}@2"


def test_the_parent_refuses_a_flag_that_is_not_a_bool():
    extractor = SubprocessContentExtractor()
    outcome = extractor._from_result({"status": "text", "text": "x", "pages_total": 0, "truncated": False,
                                      "script_rendered": "yes"})
    assert outcome.status is ExtractionStatus.ERROR and outcome.script_rendered is False


def test_the_flag_never_rides_on_a_non_text_outcome():
    extractor = SubprocessContentExtractor()
    outcome = extractor._from_result({"status": "text", "text": "  ", "pages_total": 0, "truncated": False,
                                      "script_rendered": True})
    assert outcome.status is ExtractionStatus.NO_TEXT and outcome.script_rendered is False


async def test_real_child_end_to_end(tmp_path):
    source = tmp_path / "app.html"
    source.write_text(_SCRIPT_PAGE, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    outcome = await SubprocessContentExtractor(temp_root=str(work)).extract(
        ExtractionRequest(path=str(source), name="app.html", kind="html", mime_type="text/html", user_id="u",
                          account_id="a"))
    assert outcome.status is ExtractionStatus.TEXT and outcome.script_rendered is True
