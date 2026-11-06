"""The one link rule for text a model reads (owner decisions O-2 (b) and O-6, 2026-11-05): an external http(s) URL
without `utm_*` parameters and fragment, cut at 300 characters with "…", written as "text (url)". Synthetic URLs on
example.org only."""
import pytest

from src.domain.link_url import LINK_URL_MAX, cut_url, external_url, link_with_url, text_holds_url


def test_the_cap_is_300_characters():
    assert LINK_URL_MAX == 300


def test_utm_parameters_go_and_the_others_stay_in_order():
    url = "https://example.org/offer?utm_source=news&id=7&UTM_Medium=mail&pwd=xyz&utm_campaign=autumn"
    assert external_url(url) == "https://example.org/offer?id=7&pwd=xyz"


def test_a_query_of_only_utm_parameters_leaves_no_question_mark():
    assert external_url("https://example.org/offer?utm_source=news&utm_medium=mail") == "https://example.org/offer"


def test_a_parameter_that_only_contains_utm_is_kept():
    assert external_url("https://example.org/offer?ref=utm_x&xutm_a=1") == "https://example.org/offer?ref=utm_x&xutm_a=1"


def test_the_fragment_goes():
    assert external_url("https://example.org/offer?id=7&utm_source=news#top") == "https://example.org/offer?id=7"


def test_path_and_port_are_kept_and_the_scheme_is_lower_cased():
    assert external_url("HTTP://example.org:8080/a/b/") == "http://example.org:8080/a/b/"


def test_a_scheme_relative_link_is_read_as_https():
    assert external_url("//example.org/map") == "https://example.org/map"


@pytest.mark.parametrize("href", [
    "/archive/11.html", "offer.html", "#top", "mailto:office@example.org", "javascript:alert(1)",
    "data:text/html;base64,AAAA", "ftp://example.org/file", "https://", "http://[::1", "",
])
def test_anything_but_an_http_link_with_a_host_has_no_url(href):
    assert external_url(href) == ""


def test_a_url_at_the_cap_is_kept_whole():
    url = "https://example.org/" + "a" * (LINK_URL_MAX - len("https://example.org/"))
    assert len(url) == LINK_URL_MAX
    assert cut_url(url) == url


def test_a_longer_url_is_cut_at_the_cap_with_an_ellipsis():
    url = "https://example.org/" + "a" * 380
    assert cut_url(url) == url[:LINK_URL_MAX] + "…"


def test_text_holds_the_url_with_or_without_scheme_or_trailing_slash():
    assert text_holds_url("https://example.org/agenda/", "https://example.org/agenda/")
    assert text_holds_url("see example.org/agenda/", "https://example.org/agenda/")
    assert text_holds_url("see example.org/agenda", "https://example.org/agenda/")
    assert not text_holds_url("Agenda", "https://example.org/agenda/")


def test_an_external_link_is_its_text_and_its_cleaned_url():
    assert link_with_url("Autumn offer", "https://example.org/offer?id=7&utm_source=news#top") == (
        "Autumn offer (https://example.org/offer?id=7)")


def test_a_long_link_is_written_cut():
    url = "https://example.org/" + "a" * 380
    assert link_with_url("Library opening hours", url) == f"Library opening hours ({url[:LINK_URL_MAX]}…)"


def test_surrounding_whitespace_of_the_href_is_ignored():
    assert link_with_url("Agenda", "  https://example.org/agenda  ") == "Agenda (https://example.org/agenda)"


@pytest.mark.parametrize("text,href", [
    ("Write us", "mailto:office@example.org"),
    ("Last issue", "/archive/11.html"),
    ("Click", "javascript:alert(1)"),
    ("", "https://example.org/agenda"),
    ("https://example.org/agenda", "https://example.org/agenda?utm_source=news"),
])
def test_a_link_that_keeps_its_text_only_gets_nothing(text, href):
    assert link_with_url(text, href) == ""
