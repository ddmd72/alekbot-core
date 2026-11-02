"""G1b in the drive parser: a Markdown or text file's `data:` images never reach the indexed text."""
from src.adapters.drive_parser.readers import read_content
from src.domain.drive_extraction import ExtractionStatus

B64 = "A" * 5000


def test_a_markdown_file_reads_without_base64(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(f"# Garden plan 2026/27\n![north bed](data:image/png;base64,{B64})\nRoses in March.\n")
    out = read_content(str(path), "markdown")
    assert out.status is ExtractionStatus.TEXT
    assert "north bed" in out.text and "Roses in March." in out.text
    assert "base64" not in out.text and B64 not in out.text


def test_a_text_file_reads_without_base64(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text(f"Plan ![](data:image/png;base64,{B64}) end")
    out = read_content(str(path), "text")
    assert out.text == "Plan  end"


def test_a_prefix_cut_inside_a_data_uri_ends_cleanly(tmp_path):
    path = tmp_path / "notes.md"
    path.write_bytes(b"Intro\n![bed](data:image/png;base64," + b"A" * (9 * 1024 * 1024))
    out = read_content(str(path), "markdown")
    assert out.status is ExtractionStatus.TEXT
    assert out.text.endswith("bed") and "base64" not in out.text


def test_a_json_file_is_untouched(tmp_path):
    path = tmp_path / "data.json"
    body = f'{{"img": "![x](data:image/png;base64,{B64})"}}'
    path.write_text(body)
    out = read_content(str(path), "json")
    assert B64 in out.text
