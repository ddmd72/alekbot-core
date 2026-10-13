"""Parser@2 after review (USER_DRIVE_INDEXING_RFC §25.3; review findings M1–M3, m1–m5, arbiter thresholds):
Markdown or text is HTML only with a document marker or a tag share over 30 % outside code; `script_rendered` needs
under 500 visible characters and a <script>; content is the visible <main>, else the largest <article> holding at
least half the body's text, else the body without its top-level header, footer, nav and aside. Synthetic pages."""
import base64
import os
import time

import pytest

from src.adapters.drive_parser import html_text, readers
from src.adapters.drive_parser.html_text import (
    HTML_TAG_SHARE_MIN, SCRIPT_RENDERED_TEXT_MAX, html_page_text, tag_share, text_is_html, without_code,
)
from src.adapters.drive_parser.readers import read_content
from src.domain.drive_extraction import ExtractionStatus

_BADGE_README = """<p align="center">
  <img src="https://raw.githubusercontent.com/acme/widget/main/docs/logo.png" alt="Widget logo" width="200">
</p>
<p align="center">
  <a href="https://github.com/acme/widget/actions"><img src="https://github.com/acme/widget/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/widget"><img src="https://img.shields.io/pypi/v/widget.svg" alt="PyPI"></a>
  <a href="https://codecov.io/gh/acme/widget"><img src="https://codecov.io/gh/acme/widget/branch/main/graph/badge.svg" alt="coverage"></a>
</p>

# Widget

Widget turns CSV files into charts.

## Install

```bash
pip install widget
widget --input data.csv --out chart.png
```

## Usage

- Load a file
- Pick a column
- Export

## License

MIT"""

_NOTES_QUOTING_HTML = """# Meeting notes 2024-03-02

Discussed the landing page. Alice proposed this snippet:

```html
<!DOCTYPE html>
<div class="hero"><h1>Welcome</h1><p>Buy now</p><button onclick="go()">Go</button></div>
<script>function go(){location.href='/buy'}</script>
```

Action items:
1. Bob: fix the button
2. Alice: copy for the hero"""

_JAVA_NOTES = """# Generics cheat sheet

```java
Map<String, List<Integer>> m = new HashMap<>();
List<String> names = new ArrayList<String>();
Optional<User> u = repo.find(id);
Future<Void> f; Callable<T> c; Supplier<T> s; Function<A, B> fn; Set<Long> ids;
```
Use `List<T>` over raw types, and `<html>` only in templates."""

_PROSE = "The lease runs from March 2024 to February 2025 at 950 EUR per month, deposit two months. "


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# =============================================================================
# M1: when Markdown or text is HTML
# =============================================================================

def test_the_thresholds():
    assert HTML_TAG_SHARE_MIN == 0.30 and SCRIPT_RENDERED_TEXT_MAX == 500


@pytest.mark.parametrize("text", [_BADGE_README, _NOTES_QUOTING_HTML, _JAVA_NOTES], ids=["badges", "notes", "java"])
def test_ordinary_markdown_is_kept_verbatim(tmp_path, text):
    assert text_is_html(text) is False
    outcome = read_content(str(_write(tmp_path, "README.md", text)), "markdown")
    assert outcome.status is ExtractionStatus.TEXT and outcome.text == text and outcome.script_rendered is False


def test_generics_survive(tmp_path):
    text = read_content(str(_write(tmp_path, "java.md", _JAVA_NOTES)), "markdown").text
    assert "Map<String, List<Integer>> m" in text and "`List<T>`" in text


@pytest.mark.parametrize("marker", ["<!DOCTYPE html>", "<html lang='en'>", "<HEAD>", "<body class=x>"])
def test_a_document_marker_outside_code_makes_it_html(marker):
    assert text_is_html(f"Saved page\n{marker}\n<p>{_PROSE}</p>") is True


def test_a_marker_inside_code_does_not():
    assert text_is_html("Look:\n\n```\n<!DOCTYPE html><html><body>x</body></html>\n```\n\nand `<html>` too.") is False


def test_a_tag_heavy_fragment_without_markers_is_html():
    fragment = "<div><table><tr><td>Hi</td><td>there</td></tr></table><ul><li>a</li><li>b</li></ul></div>"
    assert tag_share(fragment) > HTML_TAG_SHARE_MIN and text_is_html(fragment) is True


def test_the_share_counts_tag_names_not_attribute_values():
    assert tag_share('<img src="https://example.org/' + "x" * 200 + '.png">' + "words " * 10) < 0.1


def test_code_is_removed_fenced_tilde_unclosed_and_inline():
    text = "a `<b>x</b>` b\n~~~\n<div>\n~~~\nc\n```\n<p>never closed"
    assert "<" not in without_code(text) and "a" in without_code(text) and "c" in without_code(text)


def test_an_html_page_saved_as_text_is_still_converted(tmp_path):
    page = f"<!DOCTYPE html><html><head><title>Saved</title></head><body><p>{_PROSE * 8}</p></body></html>"
    outcome = read_content(str(_write(tmp_path, "saved.txt", page)), "text")
    assert outcome.text.startswith("Title: Saved") and "<p>" not in outcome.text


# =============================================================================
# M2: script_rendered
# =============================================================================

def _single_file_lease(with_script: bool) -> str:
    image = base64.b64encode(os.urandom(600_000)).decode()
    css = "<style>" + ".c{color:red;margin:0 auto;padding:1px}\n" * 10_000 + "</style>"
    script = "<script>window.x = 1;</script>" if with_script else ""
    return (f"<html><head><title>Lease</title>{css}{script}</head><body><main><h1>Lease agreement</h1>"
            f"<p>{_PROSE * 60}</p><img src='data:image/jpeg;base64,{image}' alt='signature'></main></body></html>")


@pytest.mark.parametrize("with_script", [False, True])
def test_a_single_file_saved_page_is_not_script_rendered(with_script):
    page = _single_file_lease(with_script)
    assert len(page.encode()) > 1_000_000
    text, flagged = html_page_text(page)
    assert flagged is False and "Lease agreement" in text


def test_a_short_page_without_scripts_is_not_script_rendered():
    assert html_page_text("<html><body><p>Short note.</p></body></html>") == ("Short note.", False)


def test_a_short_page_with_a_script_is_script_rendered():
    text, flagged = html_page_text("<html><head><title>App</title><script src='a.js'></script></head>"
                                   "<body><div id='root'>Loading</div></body></html>")
    assert flagged is True and text == "Title: App\n\nLoading"


def test_the_visible_text_bound_is_exclusive():
    body = "x" * SCRIPT_RENDERED_TEXT_MAX
    assert html_page_text(f"<html><body><script></script><p>{body}</p></body></html>")[1] is False
    assert html_page_text(f"<html><body><script></script><p>{body[1:]}</p></body></html>")[1] is True


# =============================================================================
# M3: which part of the page is the content
# =============================================================================

def test_a_widget_article_does_not_replace_the_content():
    page = ("<html><head><title>My trip notes</title></head><body>"
            "<div id='content'><h1>Trip to Valencia</h1><p>We booked the hotel Las Arenas for 3 nights, total 540 EUR."
            "</p><p>Passport number in the safe.</p></div>"
            "<div class='related'><article><h3>Related: Paella recipe</h3></article></div></body></html>")
    text, _ = html_page_text(page)
    assert "540 EUR" in text and "Passport number in the safe." in text and "Related: Paella recipe" in text


def test_the_largest_article_holding_half_the_text_is_the_content():
    page = ("<html><body><div class='menu'>Home About</div>"
            f"<article><h1>Story</h1><p>{_PROSE * 4}</p></article>"
            "<article><p>Comment: nice</p></article></body></html>")
    text, _ = html_page_text(page)
    assert text.startswith("# Story") and "Comment: nice" not in text and "Home About" not in text


def test_nested_headers_footers_and_asides_are_kept():
    page = ("<html><body><header>Site banner</header>"
            "<section><header><h2>Invoice 2024-117</h2><p>Due 2024-11-30</p></header><p>Amount 1 200 EUR</p>"
            "<footer>Signed by the landlord</footer></section>"
            "<div><aside class='note'>Important: pay via IBAN ES12 3456</aside><p>Thanks</p></div>"
            "<footer>Copyright footer</footer></body></html>")
    text, _ = html_page_text(page)
    for kept in ("Invoice 2024-117", "Due 2024-11-30", "Signed by the landlord", "pay via IBAN ES12 3456", "Thanks"):
        assert kept in text
    assert "Site banner" not in text and "Copyright footer" not in text


def test_layout_wrappers_still_lose_their_boilerplate():
    page = ("<html><body><div id='page'><header><nav>Home News</nav></header><div id='content'><p>Body text here.</p>"
            "</div><aside>Ads here</aside><footer>Footer words</footer></div></body></html>")
    text, _ = html_page_text(page)
    assert text == "Body text here."


def test_landmark_roles_count_as_their_elements():
    page = ("<html><body><div role='banner'>Banner words</div><div role='navigation'>Nav words</div>"
            "<p>Content.</p><div role='contentinfo'>Info words</div></body></html>")
    assert html_page_text(page)[0] == "Content."


def test_navigation_inside_main_is_main_content():
    page = "<html><body><nav>Top</nav><main><nav>Step 1 of 3</nav><p>Form text.</p></main></body></html>"
    text, _ = html_page_text(page)
    assert "Step 1 of 3" in text and "Top" not in text


def test_a_hidden_main_is_skipped():
    page = ("<html><body><main hidden><p>Old content</p></main><main><p>Current content</p></main>"
            "</body></html>")
    assert html_page_text(page)[0] == "Current content"


# =============================================================================
# Minor findings
# =============================================================================

def test_unclosed_comments_are_linear():
    text = "<!--x" * 200_000
    started = time.process_time()
    assert tag_share(text) == 1.0
    text_is_html(text)
    assert time.process_time() - started < 2.0


def test_the_share_is_measured_on_the_original_text():
    text = "İ" * 100 + "<b>x</b>"
    assert tag_share(text) == pytest.approx(len("<b></b>") / len(text))


def test_a_text_file_falls_back_to_its_text_when_the_html_path_fails(tmp_path, monkeypatch):
    page = "<!DOCTYPE html><html><body><p>Fallback text.</p></body></html>"

    def broken(markup):
        raise RecursionError("deep")

    monkeypatch.setattr(readers, "html_page_text", broken)
    outcome = read_content(str(_write(tmp_path, "page.md", page)), "markdown")
    assert outcome.status is ExtractionStatus.TEXT and outcome.text == page


def test_a_memory_failure_on_the_html_path_still_propagates(tmp_path, monkeypatch):
    def exhausted(markup):
        raise MemoryError

    monkeypatch.setattr(readers, "html_page_text", exhausted)
    with pytest.raises(MemoryError):
        read_content(str(_write(tmp_path, "page.md", "<!DOCTYPE html><p>x</p>")), "markdown")


def test_selects_and_hidden_elements_are_dropped():
    page = ("<html><body><main><h1>Contact</h1><form><label>Country</label><select>"
            + "".join(f"<option>Country{i}</option>" for i in range(30))
            + "</select></form><p hidden>Hidden text</p><span aria-hidden='true'>Icon text</span>"
            "<p>Visible text.</p></main></body></html>")
    text, _ = html_page_text(page)
    assert "Country0" not in text and "Hidden text" not in text and "Icon text" not in text
    assert "Country" in text and "Visible text." in text


def test_in_flow_page_links_keep_their_text_link_blocks_go():
    page = ("<html><body><main><ul><li><a href='#s1'>Parties</a></li><li><a href='#s3'>Payment</a></li></ul>"
            "<p>See <a href='#s3'>Section 3: Payment terms</a> for the dates.</p>"
            "<p><a href='#top'>Back to top</a></p></main></body></html>")
    text, _ = html_page_text(page)
    assert "See Section 3: Payment terms for the dates." in text
    assert "Parties" not in text and "Back to top" not in text


def test_the_reader_module_uses_the_html_module():
    assert readers.html_page_text is html_text.html_page_text
