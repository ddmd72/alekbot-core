"""The tool texts and the parser cannot drift (USER_DRIVE_INDEXING_RFC §29.8): every `from N` example the descriptions
quote parses as a page request, and the open description carries the cap."""
import re

from src.domain import drive_tool_texts as tt
from src.domain.drive_excerpt_request import range_request
from src.domain.drive_index_params import OPEN_MAX_CHARS


def _declaration(name):
    return next(d for d in tt.drive_tool_declarations() if d["name"] == name)


def _quoted_pages(text):
    return re.findall(r'"(from (?:N|\d+))"', text.replace('\\"', '"'))


def test_the_examples_in_the_descriptions_parse_as_pages():
    read = _declaration(tt.READ_PASSAGES)
    texts = [read["description"], read["parameters"]["properties"]["passages"]["description"]]
    examples = [e for text in texts for e in _quoted_pages(text)]
    assert "from 1" in examples and "from N" in examples
    for example in examples:
        request = range_request("drive:abc", example.replace("N", "11"))
        assert request.page and request.first >= 1


def test_the_passages_field_still_advertises_numbers_and_ranges():
    description = _declaration(tt.READ_PASSAGES)["parameters"]["properties"]["passages"]["description"]
    assert "6-8" in description
    assert range_request("drive:abc", "6-8").page is False


def test_read_passages_states_its_exclusion():
    description = _declaration(tt.READ_PASSAGES)["description"]
    assert tt.FIND_PASSAGES in description and "Not for what a file says about something" in description


def test_the_open_description_holds_the_formatted_cap():
    assert f"{OPEN_MAX_CHARS:,}" in _declaration(tt.OPEN_DRIVE_FILE)["description"]
