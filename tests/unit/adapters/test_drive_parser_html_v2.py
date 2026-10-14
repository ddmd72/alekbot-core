"""Parser@2's HTML policy (USER_DRIVE_INDEXING_RFC §25.3 "Parser@2", D23): HTML inside Markdown or text goes
through the HTML converter when tags pass a 5 % share; a page keeps <main>/<article> (else the body without nav,
header, footer, aside); in-page `#` links are dropped; external links are kept as text plus domain; title, meta
description, og:* and lang are written as plain lines at the head; a page whose text is under 1 % of its bytes is
flagged `script_rendered`. All fixtures are synthetic."""
import re

import pytest

from src.adapters.drive_parser.html_text import (
    HTML_TAG_SHARE_MIN, SCRIPT_RENDERED_TEXT_MAX, html_page_text, tag_share, text_is_html,
)
from src.adapters.drive_parser.readers import read_content
from src.domain.drive_extraction import ExtractionStatus

_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?/?>|<!--|<!DOCTYPE", re.IGNORECASE)

_NEWSLETTER = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Harbour Weekly - Issue 12</title>
  <meta name="description" content="Ferry timetable changes and the new market hall.">
  <meta property="og:title" content="Harbour Weekly #12">
  <meta property="og:site_name" content="Harbour Weekly">
  <meta property="og:image" content="https://cdn.example.org/cover.png">
  <style>.menu{display:flex}</style>
</head>
<body>
  <a href="#content">Skip to content</a>
  <header><div class="logo">HARBOUR WEEKLY LOGO</div>
    <nav class="menu"><a href="/news">News menu item</a> | <a href="/events">Events menu item</a>
      | <a href="https://www.example.org/shop">Shop menu item</a></nav>
  </header>
  <div id="content">
    <h1>Ferry timetable changes</h1>
    <p>From the first of May the morning ferry leaves at 07:40 instead of 07:15.</p>
    <p>The evening ferry keeps its timetable, and the night boat runs on Fridays and Saturdays only. Tickets bought
      before May stay valid on every crossing until the end of June; season passes are renewed at the harbour office,
      which now opens at eight in the morning on weekdays and at ten on Saturdays.</p>
    <p>Contents: <a href="#market">Market hall</a></p>
    <h2 id="market">The new market hall</h2>
    <p>The market hall opens on Saturday. <a href="https://www.example.org/market/opening?utm=1">Read the
      opening programme</a> or see <a href="/archive/11.html">last week's issue</a>.</p>
    <p><img src="data:image/png;base64,AAAA" alt="Market hall facade"></p>
    <p><a href="#top">Back to top</a></p>
  </div>
  <aside>Sponsored: buy boat insurance today</aside>
  <footer>Unsubscribe from this list | Postal address of the sender</footer>
  <script>window.track = function () { return 1; };</script>
</body>
</html>
"""


def _page_with_main() -> str:
    return ("<html><head><title>Recipe box</title></head><body>"
            "<header>Site header words</header><nav>Top nav words</nav>"
            "<main><nav>Breadcrumb words</nav><h1>Lentil soup</h1><p>Simmer the lentils for 25 minutes.</p>"
            "<aside>Chef tip: add cumin.</aside></main>"
            "<footer>Footer words</footer></body></html>")


def _articles() -> str:
    return ("<html><body><nav>Section nav</nav>"
            "<article><header><h2>First note</h2></header><p>Alpha paragraph.</p></article>"
            "<article><h2>Second note</h2><p>Beta paragraph.</p></article>"
            "<footer>Copyright words</footer></body></html>")


def _script_only(*, with_meta: bool = True) -> str:
    head = ("<title>Trip planner</title><meta name='description' content='Plan your trip to the coast.'>"
            "<meta property='og:title' content='Trip planner app'>" if with_meta else "")
    bundle = "var a=" + "[" + ",".join(str(i) for i in range(20_000)) + "];"
    return (f"<!doctype html><html lang='es'><head>{head}<script>{bundle}</script></head>"
            f"<body><noscript>You need to enable JavaScript to run this app.</noscript><div id='root'></div>"
            f"<script src='/static/js/main.js'></script></body></html>")


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _read(path, kind):
    return read_content(str(path), kind)


# =============================================================================
# Tag share and thresholds
# =============================================================================

def test_thresholds_are_the_review_values():
    assert HTML_TAG_SHARE_MIN == 0.30 and SCRIPT_RENDERED_TEXT_MAX == 500


def test_tag_share_of_plain_markdown_is_zero():
    assert tag_share("# Title\n\nPrices: 3 < 5 and 7 > 2, see <https://example.org>.") == 0.0


def test_script_and_style_bodies_count_as_markup():
    page = "<html><head><script>" + "var x = 1;" * 500 + "</script><style>" + "p{}" * 300 + "</style></head></html>"
    assert tag_share(page) == 1.0
    assert tag_share("<script>unclosed " + "x" * 100) == 1.0
    assert tag_share("plain words " * 100 + "<SCRIPT type='a'>" + "y" * 20 + "</Script >") < 0.05


def test_tag_share_of_a_page_is_high_and_bounded():
    share = tag_share(_NEWSLETTER)                            # tag names only since review M1; the doctype decides
    assert 0.0 < share <= 1.0 and text_is_html(_NEWSLETTER)
    assert tag_share("") == 0.0


# =============================================================================
# HTML inside Markdown or text
# =============================================================================

@pytest.mark.parametrize("kind", ["markdown", "text"])
def test_a_markdown_or_text_file_that_is_a_whole_page_is_converted(tmp_path, kind):
    outcome = _read(_write(tmp_path, "saved.md", _NEWSLETTER), kind)
    assert outcome.status is ExtractionStatus.TEXT
    assert _TAG.search(outcome.text) is None                  # tags are 0 % of the stored text
    assert tag_share(outcome.text) == 0.0
    assert "From the first of May the morning ferry leaves at 07:40" in outcome.text
    assert "window.track" not in outcome.text and "display:flex" not in outcome.text
    assert outcome.script_rendered is False


def test_markdown_with_a_little_inline_html_stays_verbatim(tmp_path):
    body = "# Notes\n\n" + " ".join(["Plain paragraph about the garden and the tomatoes."] * 30) + "\n\nLine<br>break\n"
    assert tag_share(body) < HTML_TAG_SHARE_MIN
    assert _read(_write(tmp_path, "notes.md", body), "markdown").text == body.strip()


def test_json_is_never_sent_through_the_html_converter(tmp_path):
    body = '{"html": "<div><p>x</p><p>y</p></div>"}'
    assert tag_share(body) > HTML_TAG_SHARE_MIN
    assert _read(_write(tmp_path, "a.json", body), "json").text == body


# =============================================================================
# Navigation and boilerplate, links
# =============================================================================

def test_newsletter_menu_header_footer_and_aside_are_dropped(tmp_path):
    text = _read(_write(tmp_path, "issue.html", _NEWSLETTER), "html").text
    for boilerplate in ("News menu item", "Events menu item", "Shop menu item", "HARBOUR WEEKLY LOGO",
                        "Sponsored: buy boat insurance", "Unsubscribe from this list"):
        assert boilerplate not in text
    assert "# Ferry timetable changes" in text and "## The new market hall" in text   # headings stay Markdown


def test_in_page_links_are_dropped(tmp_path):
    text = _read(_write(tmp_path, "issue.html", _NEWSLETTER), "html").text
    assert "Skip to content" not in text and "Back to top" not in text
    assert "Contents: Market hall" in text and "(#" not in text       # in a sentence the text stays (review m5)


def test_external_links_keep_their_text_and_domain_relative_links_their_text(tmp_path):
    text = _read(_write(tmp_path, "issue.html", _NEWSLETTER), "html").text
    assert "Read the opening programme (example.org)" in " ".join(text.split())
    assert "last week's issue" in text
    assert "](" not in text and "utm=" not in text and "/archive/11.html" not in text


def test_images_become_their_alt_text(tmp_path):
    text = _read(_write(tmp_path, "issue.html", _NEWSLETTER), "html").text
    assert "Market hall facade" in text and "data:image" not in text and "![" not in text


def test_main_is_kept_with_its_own_navigation(tmp_path):
    text = _read(_write(tmp_path, "soup.html", _page_with_main()), "html").text
    assert "Simmer the lentils for 25 minutes." in text and "Chef tip: add cumin." in text
    assert "Breadcrumb words" in text                         # §25.3 keeps <main> whole (review M3)
    for gone in ("Site header words", "Top nav words", "Footer words"):
        assert gone not in text


def test_articles_are_kept_with_their_own_headers(tmp_path):
    text = _read(_write(tmp_path, "notes.html", _articles()), "html").text
    assert "First note" in text and "Alpha paragraph." in text and "Beta paragraph." in text
    assert "Section nav" not in text and "Copyright words" not in text
    assert text.index("Alpha paragraph.") < text.index("Beta paragraph.")


def test_an_empty_main_falls_back_to_the_body(tmp_path):
    page = "<html><body><nav>Menu</nav><main></main><div><p>Real text in a div.</p></div></body></html>"
    text = _read(_write(tmp_path, "p.html", page), "html").text
    assert "Real text in a div." in text and "Menu" not in text


# =============================================================================
# Metadata lines at the head
# =============================================================================

def test_title_description_og_and_language_lead_the_text(tmp_path):
    text = _read(_write(tmp_path, "issue.html", _NEWSLETTER), "html").text
    head = text.split("\n\n", 1)[0].splitlines()
    assert head == ["Title: Harbour Weekly - Issue 12",
                    "Description: Ferry timetable changes and the new market hall.",
                    "og:title: Harbour Weekly #12",
                    "og:site_name: Harbour Weekly",
                    "Language: en"]
    assert "cover.png" not in text                            # a media URL is not a description


def test_og_values_equal_to_title_or_description_are_not_repeated():
    page = ("<html><head><title>Same</title><meta name='description' content='Desc'>"
            "<meta property='og:title' content='Same'><meta property='og:description' content='Desc'>"
            "</head><body><p>" + "Body text. " * 20 + "</p></body></html>")
    text, flagged = html_page_text(page)
    assert text.splitlines()[:2] == ["Title: Same", "Description: Desc"]
    assert "og:" not in text and flagged is False


def test_a_page_without_metadata_has_no_head_lines(tmp_path):
    text = _read(_write(tmp_path, "p.html", "<html><body><p>Just a paragraph.</p></body></html>"), "html").text
    assert text == "Just a paragraph."


# =============================================================================
# Script-rendered pages
# =============================================================================

def test_a_script_only_page_is_flagged_and_keeps_its_metadata(tmp_path):
    outcome = _read(_write(tmp_path, "app.html", _script_only()), "html")
    assert outcome.status is ExtractionStatus.TEXT and outcome.script_rendered is True
    assert outcome.text.splitlines()[:4] == ["Title: Trip planner", "Description: Plan your trip to the coast.",
                                             "og:title: Trip planner app", "Language: es"]
    assert "var a=" not in outcome.text and "enable JavaScript" not in outcome.text


def test_a_script_only_markdown_file_is_flagged_too(tmp_path):
    outcome = _read(_write(tmp_path, "app.md", _script_only()), "markdown")
    assert outcome.status is ExtractionStatus.TEXT and outcome.script_rendered is True


def test_a_language_alone_is_not_text():
    page = "<html lang='uk'><body><div id='root'></div></body></html>"
    assert html_page_text(page) == ("", False)              # no <script>: not script-rendered (review M2)


def test_a_script_only_page_without_metadata_is_no_text(tmp_path):
    outcome = _read(_write(tmp_path, "app.html", _script_only(with_meta=False)), "html")
    assert outcome.status is ExtractionStatus.NO_TEXT and outcome.script_rendered is False


def test_an_ordinary_page_is_not_flagged(tmp_path):
    assert _read(_write(tmp_path, "issue.html", _NEWSLETTER), "html").script_rendered is False


def test_the_share_is_measured_on_the_body_text_not_the_metadata_lines():
    page = ("<html><head><title>" + "T" * 400 + "</title><script>" + "x" * 29_000 + "</script></head>"
            "<body><p>Hi</p></body></html>")
    text, flagged = html_page_text(page)
    assert flagged is True and text.endswith("Hi")


def test_non_html_kinds_are_never_flagged(tmp_path):
    outcome = _read(_write(tmp_path, "a.txt", "x" * 10), "text")
    assert outcome.script_rendered is False
