"""Deep research drive copy — report naming (decisions/deep_research_drive_copy.md).

The copy's file name is `YYYY-MM-DD <title>.html`: the title from the report's <title>, else the research
question shortened, sanitized for the drive and capped in UTF-8 bytes (a Cyrillic character costs 2).
"""
from datetime import date

import pytest

from src.domain.deep_research_drive_copy import (
    DEEP_RESEARCH_REPORT_CONTEXT_KEY,
    MAX_REPORT_NAME_BYTES,
    MAX_QUERY_TITLE_CHARS,
    deep_research_drive_filename,
    report_title_from_html,
)
from src.domain.user_drive import sanitize_drive_filename

DAY = date(2026, 10, 14)


class TestReportTitleFromHtml:
    def test_reads_the_title(self):
        assert report_title_from_html("<html><head><title>Quantum computing</title></head></html>") == \
            "Quantum computing"

    def test_unescapes_entities_and_collapses_whitespace(self):
        html = "<title lang='uk'>\n  Квантові &amp; класичні\n   обчислення </title>"
        assert report_title_from_html(html) == "Квантові & класичні обчислення"

    def test_is_case_insensitive(self):
        assert report_title_from_html("<TITLE>Report</TITLE>") == "Report"

    @pytest.mark.parametrize("html", ["<html><body>no title</body></html>", "<title>   </title>", ""])
    def test_no_title_is_empty(self, html):
        assert report_title_from_html(html) == ""


class TestFilename:
    def test_date_title_and_extension(self):
        assert deep_research_drive_filename(DAY, "Quantum computing", "q") == "2026-10-14 Quantum computing.html"

    def test_cyrillic_title_is_kept(self):
        name = deep_research_drive_filename(DAY, "Ринок оренди у Валенсії", "q")
        assert name == "2026-10-14 Ринок оренди у Валенсії.html"

    def test_forbidden_characters_are_replaced(self):
        name = deep_research_drive_filename(DAY, 'A/B: "why"? <x> * y | z\\w', "q")
        assert name == "2026-10-14 A_B_ _why__ _x_ _ y _ z_w.html"
        assert sanitize_drive_filename(name) == name

    def test_trailing_dot_and_space_never_reach_the_extension(self):
        assert deep_research_drive_filename(DAY, "Summary...  ", "q") == "2026-10-14 Summary.html"

    def test_falls_back_to_the_query_when_there_is_no_title(self):
        assert deep_research_drive_filename(DAY, "", "Best e-bikes under 2000 EUR") == \
            "2026-10-14 Best e-bikes under 2000 EUR.html"

    def test_a_long_query_is_shortened_at_a_word(self):
        query = "Compare the long term total cost of ownership of heat pumps versus gas boilers in Springfield apartments"
        name = deep_research_drive_filename(DAY, "", query)
        stem = name[len("2026-10-14 "):-len(".html")]
        assert stem.endswith("…")
        assert len(stem) <= MAX_QUERY_TITLE_CHARS + 1
        assert query.startswith(stem[:-1].rstrip())
        assert not stem[:-1].endswith(" ")

    def test_no_title_and_no_query(self):
        assert deep_research_drive_filename(DAY, "", "   ") == "2026-10-14 report.html"

    @pytest.mark.parametrize("title", ["Дослідження " * 60, "x" * 600, "Ä" * 300 + " end"])
    def test_a_long_title_is_capped_in_bytes_on_a_character_boundary(self, title):
        name = deep_research_drive_filename(DAY, title, "q")
        encoded = name.encode("utf-8")
        assert len(encoded) <= MAX_REPORT_NAME_BYTES
        assert encoded.decode("utf-8") == name          # no split character
        assert name.startswith("2026-10-14 ") and name.endswith(".html")
        assert not name[:-len(".html")].endswith((" ", "."))
        assert sanitize_drive_filename(name) == name


def test_context_key_is_a_plain_string():
    assert isinstance(DEEP_RESEARCH_REPORT_CONTEXT_KEY, str) and DEEP_RESEARCH_REPORT_CONTEXT_KEY
