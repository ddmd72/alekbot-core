"""A passages head line's id given as a ref (USER_DRIVE_INDEXING_RFC §28.3 A7, A10): `drive:<id>:<i>-<j>` (or `:<i>`)
is the file's ref with the passage numbers dropped, in `read_passages`' file_ref and in each of `find_passages`'
file_refs. The id is taken from a real head line, as `render_excerpts` prints it."""
import re

import pytest

from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_render import head_line
from src.domain.drive_excerpt_request import NOT_A_REF, parse_file_refs, range_request
from src.domain.drive_excerpt_run import ExcerptRun
from tests.unit.domain.drive_excerpt_builders import TOMS_PLAN, excerpt_file


def _printed_id(item_id: str, first: int, last: int) -> str:
    """The id the model reads on a head line: `[id <here> · <path> · …]`."""
    line = head_line(excerpt_file(item_id, TOMS_PLAN), ExcerptRun(item_id, first, last, "text"))
    match = re.match(r"\[id (\S+) · ", line)
    assert match is not None, line
    return match.group(1)


def test_a_head_line_id_is_the_files_ref_in_range_request():
    printed = _printed_id("01ABC!123", 3, 5)
    assert printed == "drive:01ABC!123:3-5"
    assert range_request(printed, "6-8") == ExcerptRangeRequest("drive:01ABC!123", 6, 8)


def test_a_single_passage_head_line_id_is_the_files_ref():
    printed = _printed_id("item-1", 4, 4)
    assert range_request(printed, "5") == ExcerptRangeRequest("drive:item-1", 5, 5)
    assert range_request("drive:item-1:4", "5") == ExcerptRangeRequest("drive:item-1", 5, 5)


def test_head_line_ids_in_file_refs_are_refs_and_merge_with_the_plain_ref():
    printed = _printed_id("item-1", 1, 8)
    assert parse_file_refs(f"{printed}, drive:item-2:3, drive:item-1") == ("drive:item-1", "drive:item-2")
    assert parse_file_refs([printed]) == ("drive:item-1",)


def test_a_plain_ref_is_unchanged():
    assert range_request("drive:item-1", "2") == ExcerptRangeRequest("drive:item-1", 2, 2)
    assert parse_file_refs("drive:01ABC!123") == ("drive:01ABC!123",)


@pytest.mark.parametrize("value", ["drive::3-5", "drive::4"])
def test_numbers_alone_are_not_a_ref(value):
    with pytest.raises(ValueError) as refused:
        range_request(value, "1")
    assert str(refused.value) == NOT_A_REF.format(value=value)
    with pytest.raises(ValueError):
        parse_file_refs(value)
