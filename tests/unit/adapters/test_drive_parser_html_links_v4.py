"""O-2 (b), 2026-11-05: an external link in the child's HTML reader keeps its full URL, cleaned (no `utm_*`
parameters, no fragment, cut at 300 characters). One engine: HTML pages and Markdown/text files that are HTML.
All fixtures are synthetic."""
from src.adapters.drive_parser.html_text import html_page_text
from src.adapters.drive_parser.readers import read_content


def _text(body: str) -> str:
    text, _ = html_page_text(f"<html><body><main><p>{body}</p></main></body></html>")
    return " ".join(text.split())


def test_external_link_keeps_its_url():
    assert "Agenda (https://example.org/agenda?id=7)" in _text('<a href="https://example.org/agenda?id=7">Agenda</a>')


def test_utm_parameters_removed_others_kept_in_order():
    text = _text('<a href="https://example.org/meet?utm_source=mail&id=7&utm_campaign=x&pwd=xyz">Parents meeting</a>')
    assert "Parents meeting (https://example.org/meet?id=7&pwd=xyz)" in text
    assert "utm_" not in text


def test_fragment_removed():
    text = _text('<a href="https://example.org/meet?pwd=xyz#top">Join</a>')
    assert "Join (https://example.org/meet?pwd=xyz)" in text and "#top" not in text


def test_long_url_cut_at_300_with_ellipsis():
    url = "https://example.org/" + "a" * 380
    text = _text(f'<a href="{url}">Library opening hours</a>')
    assert f"({url[:300]}…)" in text
    assert url[:301] not in text


def test_link_text_equal_to_url_is_not_duplicated():
    url = "https://example.org/agenda?id=7"
    text = _text(f'<a href="{url}">{url}</a>')
    assert text.count("example.org/agenda") == 1


def test_link_text_holding_the_url_is_not_duplicated():
    text = _text('<a href="https://example.org/agenda?utm_source=mail">see example.org/agenda</a>')
    assert text.count("example.org/agenda") == 1


def test_protocol_relative_link_becomes_https():
    assert "Map (https://example.org/x)" in _text('<a href="//example.org/x">Map</a>')


def test_relative_and_mailto_links_keep_their_text_only():
    text = _text('<a href="/archive/11.html">Last issue</a> and <a href="mailto:office@example.org">Write us</a>')
    assert "Last issue" in text and "Write us" in text
    assert "archive/11" not in text and "mailto" not in text and "office@" not in text


def test_in_page_links_block_dropped_and_in_sentence_link_keeps_text():
    markup = ('<html><body><main><p><a href="#a">Contents A</a> <a href="#b">Contents B</a></p>'
              '<p>See the <a href="#hours">opening hours</a> below.</p></main></body></html>')
    text = " ".join(html_page_text(markup)[0].split())
    assert "Contents A" not in text and "Contents B" not in text
    assert "See the opening hours below." in text and "#hours" not in text


def test_javascript_and_data_hrefs_absent():
    text = _text('<a href="javascript:alert(1)">Click</a> <a href="data:text/html;base64,AAAA">Blob</a>')
    assert "Click" in text and "Blob" in text
    assert "javascript" not in text and "data:" not in text


def test_markdown_file_that_is_html_uses_the_same_form(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("<!DOCTYPE html><html><body><main><p>Example School parents' meeting, March 3, 2026: "
                    '<a href="https://example.org/agenda?id=7&utm_source=mail#top">agenda</a></p></main></body></html>',
                    encoding="utf-8")
    text = " ".join(read_content(str(path), "markdown").text.split())
    assert "agenda (https://example.org/agenda?id=7)" in text
