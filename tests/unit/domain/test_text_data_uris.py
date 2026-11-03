"""G1b: a text file's inline `data:` images are dropped to their alt text; nothing else changes."""
import re
import time
from pathlib import Path

import pytest

from src.domain.drive_index_params import MAX_TEXT_CHARS
from src.domain.text_data_uris import drop_data_uri_images
from src.domain.document_limits import MAX_TEXT_EXPORT_BYTES

_PROVIDER_RE = re.compile(r"(?<![a-z])(onedrive|graph|microsoft|google|gdrive|approot|quickxor|ctag|takendatetime)",
                          re.IGNORECASE)
B64 = "iVBORw0KGgo" * 50


def test_the_export_cap_is_ten_megabytes():
    assert MAX_TEXT_EXPORT_BYTES == 10 * 1024 * 1024


def test_text_without_data_uris_is_returned_unchanged():
    text = "Garden plan 2026/27\n![sketch](https://example.com/a.png)\n"
    assert drop_data_uri_images(text) == text


def test_an_inline_image_becomes_its_alt():
    assert drop_data_uri_images(f"Beds: ![north bed](data:image/png;base64,{B64}) done") == "Beds: north bed done"


def test_an_inline_image_with_a_title_becomes_its_alt():
    out = drop_data_uri_images(f'a ![plan](data:image/png;base64,{B64} "Garden plan 2026/27") b')
    assert out == "a plan b"


def test_an_empty_alt_leaves_nothing():
    assert drop_data_uri_images(f"x![](data:image/png;base64,{B64})y") == "xy"


def test_a_reference_definition_is_removed_and_its_uses_become_alt():
    text = (f"Intro\n![bed][img1] and ![][img2] and ![img1]\n\n[img1]: <data:image/png;base64,{B64}>\n"
            f"[IMG2]: data:image/png;base64,{B64}\nEnd\n")
    out = drop_data_uri_images(text)
    assert out == "Intro\nbed and  and img1\n\nEnd\n"


def test_a_reference_to_an_http_definition_stays():
    text = "![a][x]\n\n[x]: https://example.com/a.png\n"
    assert drop_data_uri_images(text) == text


@pytest.mark.parametrize("tag", [
    f'<img src="data:image/png;base64,{B64}" alt="roses">',
    f"<img alt='roses' src='data:image/png;base64,{B64}'>",
    f'<IMG width="3" alt="roses" src="data:image/png;base64,{B64}" />',
])
def test_an_html_image_becomes_its_alt(tag):
    assert drop_data_uri_images(f"a {tag} b") == "a roses b"


def test_an_html_image_without_alt_leaves_nothing_and_an_http_one_stays():
    assert drop_data_uri_images(f'a<img src="data:image/png;base64,{B64}">b') == "ab"
    http = '<img src="https://example.com/a.png" alt="x">'
    assert drop_data_uri_images(http) == http


def test_an_unterminated_trailing_image_keeps_its_alt_and_drops_the_rest():
    assert drop_data_uri_images(f"Notes\n![bed](data:image/png;base64,{B64}") == "Notes\nbed"


def test_a_non_image_data_link_and_an_https_image_stay():
    text = f"[file](data:text/plain;base64,{B64}) and ![x](https://example.com/x.png)"
    assert drop_data_uri_images(text) == text


def test_several_images_in_one_text():
    one = f"![a](data:image/png;base64,{B64})"
    assert drop_data_uri_images(f"{one} mid {one}") == "a mid a"


def test_a_four_megabyte_export_shrinks_below_the_text_cap():
    line = f"Bed notes\n![](data:image/png;base64,{'A' * 1_000_000})\n"
    text = line * 4
    assert len(text) > 4_000_000
    out = drop_data_uri_images(text)
    assert out == "Bed notes\n\n" * 4
    assert len(out) < MAX_TEXT_CHARS


@pytest.mark.timing
def test_ten_megabytes_run_in_under_a_second():
    block = f"text line\n![](data:image/png;base64,{'A' * 100_000})\n"
    text = block * (10 * 1024 * 1024 // len(block))
    started = time.perf_counter()
    drop_data_uri_images(text)
    assert time.perf_counter() - started < 1.0


def test_the_module_source_holds_no_provider_word():
    source = (Path(__file__).resolve().parents[3] / "src/domain/text_data_uris.py").read_text()
    assert _PROVIDER_RE.search(source) is None
