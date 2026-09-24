"""An escaped newline after a URL ends the URL — it never pulls the next line into it."""
from src.domain.result_links import extract_result_links


def test_maps_result_rendered_as_python_repr_keeps_each_url_on_its_own_line():
    # MapsSearchAgent returns {"text": ...}; _format_result renders it with str(dict), so the
    # newlines arrive as the two characters "\n" and the flat JSON parse cannot apply.
    result = str({"text": "1. Cafe A — https://maps.google.com/?cid=1\n2. Cafe B — https://maps.google.com/?cid=2"})

    urls = [link["url"] for link in extract_result_links(result)]

    assert urls == ["https://maps.google.com/?cid=1", "https://maps.google.com/?cid=2"]


def test_json_escaped_newline_after_a_url_is_not_part_of_it():
    # Nested braces defeat the flat-object parse, so the URL lands on the bare-URL path.
    result = r'{"text": "see https://example.com/a\/b\nnext line {x}"}'

    urls = [link["url"] for link in extract_result_links(result)]

    assert urls == ["https://example.com/a/b"]
