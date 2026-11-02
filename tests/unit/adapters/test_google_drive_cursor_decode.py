"""The Google drive feed cursor: `decode` accepts what `encode` wrote and refuses everything else with ValueError
(a refused cursor is a resync, never a guess)."""
import json

import pytest

from src.adapters.google.google_drive_cursor import ENUMERATE, FEED, FeedCursor, corpora_of, decode, encode

_DRIVE = "google_drive:account:perm-a"


def _good(**over):
    data = {"v": 1, "drive": _DRIVE, "phase": ENUMERATE, "start": "7", "corpora": [{"c": "user"}], "list": "", "page": ""}
    data.update(over)
    return json.dumps(data)


def test_what_encode_wrote_decodes_to_the_same_cursor():
    cursor = FeedCursor(drive=_DRIVE, phase=ENUMERATE, start="7",
                        corpora=corpora_of([("drive-1", "Acme Hamburg")]), list_token="lt.0.2")
    assert decode(encode(cursor)) == cursor
    feed = FeedCursor(drive=_DRIVE, phase=FEED, start="7", page="9")
    assert decode(encode(feed)) == feed


@pytest.mark.parametrize("raw", [
    _good(corpora="user"),                                               # corpora mistyped (not a list)
    _good(corpora=["user"]),                                             # a corpus that is not an object
    _good(corpora=[{"c": "network"}]),                                   # an unknown corpus
    _good(corpora=[{"c": "user", "id": "x"}]),                           # a user corpus with extra fields
    _good(corpora=[{"c": "drive", "id": "", "name": "Acme Hamburg"}]),   # a drive corpus without an id
    _good(corpora=[{"c": "drive", "id": "drive-1", "name": 3}]),         # a drive corpus with a mistyped name
    _good(corpora=[{"c": "drive", "id": "drive-1"}]),                    # a drive corpus without a name
    _good(start=7),                                                      # a mistyped string field
    _good(list=None),
    _good(page=1),
    _good(drive=""),                                                     # an empty drive
    _good(phase="sideways"),                                             # an unknown phase
    _good(phase=FEED, page=""),                                          # a feed cursor without a page token
    _good(v=2),                                                          # another version
    json.dumps(["not", "an", "object"]),
    "not json",
    "",
])
def test_a_malformed_cursor_is_a_value_error(raw):
    with pytest.raises(ValueError):
        decode(raw)


def test_a_non_string_is_a_value_error():
    with pytest.raises(ValueError):
        decode(None)  # type: ignore[arg-type]


def test_a_cursor_without_corpora_decodes_with_none():
    assert decode(_good(corpora=None)).corpora is None
