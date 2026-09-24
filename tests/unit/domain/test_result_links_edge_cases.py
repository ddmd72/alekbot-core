"""extract_result_links edge cases (M7): Slack-markup links and JSON-escaped URLs that fall
through to the bare-URL path."""
from src.domain.result_links import extract_result_links


def test_slack_style_link_extracts_url_and_title_separately():
    text = "See <https://x.y/path|Title> for the source."
    links = extract_result_links(text)
    assert links == [{"anchor": 1, "title": "Title", "url": "https://x.y/path"}]
    # The pipe and title must never leak into the URL itself.
    assert "|" not in links[0]["url"] and "Title" not in links[0]["url"]


def test_slack_style_link_mixed_with_a_bare_url_after_it():
    text = "Primary: <https://a.example/one|First> and also see https://b.example/two directly."
    links = extract_result_links(text)
    assert links == [
        {"anchor": 1, "title": "First", "url": "https://a.example/one"},
        {"anchor": 2, "title": "b.example", "url": "https://b.example/two"},
    ]


def test_json_escaped_url_falling_through_to_bare_path_is_unescaped():
    # The nested {inner} object defeats the flat _JSON_OBJ_RE, so the whole finding falls
    # through to the bare-URL scan while its url still carries a JSON \/ escape (e.g. a
    # double-encoded upstream payload quoted verbatim into the combined result string).
    text = 'Note: {complex {inner} object} see https://example.com\\/path for details.'
    links = extract_result_links(text)
    assert links == [{"anchor": 1, "title": "example.com", "url": "https://example.com/path"}]


def test_ampersand_in_a_bare_url_passes_through_unchanged():
    text = "Report at https://example.com/x?a=1&b=2 for the numbers."
    links = extract_result_links(text)
    assert links[0]["url"] == "https://example.com/x?a=1&b=2"


def test_flat_findings_json_path_is_unaffected_regression():
    text = (
        '{"findings": [{"text": "Whey protein", '
        '"source": "Optimum Nutrition Gold Standard — Amazon.es", '
        '"url": "https://www.amazon.es/dp/B000QSNYGI"}]}'
    )
    links = extract_result_links(text)
    assert links == [
        {"anchor": 1, "title": "Optimum Nutrition Gold Standard — Amazon.es",
         "url": "https://www.amazon.es/dp/B000QSNYGI"},
    ]
