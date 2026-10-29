"""`read_passages` page requests (USER_DRIVE_INDEXING_RFC §29.5 L9)."""
import pytest

from src.domain.drive_excerpt_page import last_page_start
from src.domain.drive_excerpt_request import BAD_RANGE, parse_page, range_request
from src.domain.drive_index_params import EXCERPT_RANGE_MAX

REF = "drive:abc123"


@pytest.mark.parametrize("text,first", [
    ("from 1", 1), ("FROM 1", 1), ("from passage 11", 11), (" from  11 ", 11), ("From Passage 5", 5),
    ("start", 1), ("beginning", 1), ("first page", 1), ("First  Page", 1),
])
def test_accepted_spellings(text, first):
    req = range_request(REF, text)
    assert req.page is True
    assert (req.first, req.last) == (first, first + EXCERPT_RANGE_MAX - 1)
    assert req.file_ref == REF


@pytest.mark.parametrize("text,span", [("7", (7, 7)), ("6-8", (6, 8)), ("3–5", (3, 5))])
def test_plain_ranges_are_not_pages(text, span):
    req = range_request(REF, text)
    assert req.page is False and (req.first, req.last) == span


@pytest.mark.parametrize("text", ["from", "from x", "from 0", "from 3-5", "from passage", "from 2 and 3"])
def test_bad_pages_get_the_range_correction(text):
    with pytest.raises(ValueError) as e:
        range_request(REF, text)
    assert str(e.value) == BAD_RANGE.format(value=text.strip())


def test_a_head_line_id_works_with_a_page():
    req = range_request("drive:abc123:3-5", "from 11")
    assert req.file_ref == REF and req.page and req.first == 11


def test_parse_page_is_none_for_ranges():
    assert parse_page("3") is None and parse_page("") is None and parse_page(None) is None


def test_last_page_start():
    assert last_page_start(25) == 16
    assert last_page_start(3) == 1
    assert last_page_start(10) == 1
    assert last_page_start(11) == 2
