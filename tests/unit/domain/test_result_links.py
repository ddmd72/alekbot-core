"""extract_result_links — pure domain function (VOICE_COMPANION_RFC §4.10 rule 2, generalized)."""
from src.domain.result_links import MAX_LINKS, extract_result_links


def test_web_search_findings_json_titles_from_source():
    text = (
        '{"findings": [{"text": "Whey protein", '
        '"source": "Optimum Nutrition Gold Standard — Amazon.es", '
        '"url": "https://www.amazon.es/dp/B000QSNYGI"}, '
        '{"text": "Another", "source": "MyProtein", '
        '"url": "https://www.myprotein.com/whey"}]}'
    )
    links = extract_result_links(text)
    assert links == [
        {"anchor": 1, "title": "Optimum Nutrition Gold Standard — Amazon.es", "url": "https://www.amazon.es/dp/B000QSNYGI"},
        {"anchor": 2, "title": "MyProtein", "url": "https://www.myprotein.com/whey"},
    ]


def test_json_embedded_in_fan_out_combined_string():
    text = (
        'Primary specialist: Web Search\n'
        '{"findings": [{"text": "a", "source": "Amazon.es listing", "url": "https://www.amazon.es/dp/B1"}]}\n'
        'Additional specialist: Maps\nNo relevant results.'
    )
    links = extract_result_links(text)
    assert links == [{"anchor": 1, "title": "Amazon.es listing", "url": "https://www.amazon.es/dp/B1"}]


def test_bare_urls_in_prose_titled_by_host():
    text = "Check https://example.com/page and also https://www.other-site.org/x for details."
    links = extract_result_links(text)
    assert links == [
        {"anchor": 1, "title": "example.com", "url": "https://example.com/page"},
        {"anchor": 2, "title": "other-site.org", "url": "https://www.other-site.org/x"},
    ]


def test_dedupe_preserves_first_occurrence_order():
    text = (
        "See https://example.com/a and https://example.com/b then again https://example.com/a."
    )
    links = extract_result_links(text)
    assert [l["url"] for l in links] == ["https://example.com/a", "https://example.com/b"]
    assert len(links) == 2


def test_no_urls_returns_empty_list():
    assert extract_result_links("Sunny, 24°C in Valencia.") == []
    assert extract_result_links("") == []


def test_trailing_punctuation_stripped():
    text = "Source: (https://example.com/thing), also see https://example.com/other."
    links = extract_result_links(text)
    assert links[0]["url"] == "https://example.com/thing"
    assert links[1]["url"] == "https://example.com/other"


def test_balanced_parens_in_url_are_kept():
    text = "See https://en.wikipedia.org/wiki/Foo_(bar) for background."
    links = extract_result_links(text)
    assert links[0]["url"] == "https://en.wikipedia.org/wiki/Foo_(bar)"


def test_cap_respected():
    text = " ".join(f"https://example.com/{i}" for i in range(MAX_LINKS + 5))
    links = extract_result_links(text)
    assert len(links) == MAX_LINKS
    assert [l["anchor"] for l in links] == list(range(1, MAX_LINKS + 1))


def test_title_field_used_when_no_source():
    text = '{"title": "Fallback Title", "url": "https://example.com/t"}'
    links = extract_result_links(text)
    assert links == [{"anchor": 1, "title": "Fallback Title", "url": "https://example.com/t"}]
