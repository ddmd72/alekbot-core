"""The tagged drive ref codec (MULTI_DRIVE_RFC §9, D13): `drive:<tag>:<item_id>` round-trips, an item id may hold `:`,
label punctuation is stripped, and an old untagged ref parses to `UntaggedRef` (never resolved, never tagged)."""
import pytest

from src.domain import drive_ref
from src.domain.drive_ref import DriveRef, is_drive_ref, is_valid_tag, parse
from src.domain.drive_untagged_ref import UntaggedRef


class TestTags:
    @pytest.mark.parametrize("tag", ["a1", "ab", "abcdef", "z9z9z9", "q0"])
    def test_valid(self, tag):
        assert is_valid_tag(tag)

    @pytest.mark.parametrize("tag", ["", "a", "abcdefg", "1a", "A1", "a-1", "a_1", "a:1", " a1", "é1"])
    def test_invalid(self, tag):
        assert not is_valid_tag(tag)

    def test_the_alphabets_match_the_rule(self):
        assert all(is_valid_tag(first + "0") for first in drive_ref.TAG_FIRST_ALPHABET)
        assert all(is_valid_tag("a" + rest) for rest in drive_ref.TAG_ALPHABET)
        assert (drive_ref.TAG_MIN_LEN, drive_ref.TAG_MAX_LEN) == (2, 6)


class TestFormatAndRoundTrip:
    def test_format(self):
        assert drive_ref.format("a1", "item-1") == "drive:a1:item-1"
        assert str(DriveRef(tag="a1", item_id="item-1")) == "drive:a1:item-1"

    @pytest.mark.parametrize("tag,item_id", [
        ("a1", "item-1"), ("abcdef", "ITEM!123"), ("b2", "x:y:z"), ("c3", "item:with:colons:"),
        ("d4", "01ABCDEF2GHIJ"),
    ])
    def test_round_trip(self, tag, item_id):
        assert parse(drive_ref.format(tag, item_id)) == DriveRef(tag=tag, item_id=item_id)

    def test_an_item_id_with_colons_keeps_them_split_on_the_first_colon_only(self):
        assert parse("drive:a1:part:two") == DriveRef(tag="a1", item_id="part:two")

    @pytest.mark.parametrize("tag,item_id", [("A1", "item-1"), ("a", "item-1"), ("a1", "")])
    def test_format_refuses_a_bad_tag_or_an_empty_item(self, tag, item_id):
        with pytest.raises(ValueError):
            drive_ref.format(tag, item_id)

    def test_two_connections_give_one_item_two_refs(self):
        assert drive_ref.format("a1", "item-1") != drive_ref.format("b2", "item-1")


class TestParseInText:
    @pytest.mark.parametrize("text", [
        "[Drive: report.pdf (1KB) ref=drive:a1:item-1]",
        '"drive:a1:item-1"',
        "'drive:a1:item-1'",
        "(drive:a1:item-1)",
        "open drive:a1:item-1, please",
        "drive:a1:item-1\nnext line",
    ])
    def test_label_punctuation_is_not_part_of_the_ref(self, text):
        assert parse(text) == DriveRef(tag="a1", item_id="item-1")

    def test_the_first_ref_wins(self):
        assert parse("drive:a1:item-1 and drive:b2:item-2") == DriveRef(tag="a1", item_id="item-1")

    @pytest.mark.parametrize("text", ["", "report.pdf", "drive_note:abc", "drive:", "drive: a1:item-1", "gcs:x/y"])
    def test_no_ref(self, text):
        assert parse(text) is None
        assert not is_drive_ref(text)


class TestOldUntaggedRefs:
    """D13: a ref from before the reorganisation is recognised as a drive ref, never resolved to a connection."""

    @pytest.mark.parametrize("text,raw", [
        ("drive:01ABCDEF2GHIJ", "drive:01ABCDEF2GHIJ"),
        ("ref=drive:ABC123!456]", "drive:ABC123!456"),
        ("drive:item-1", "drive:item-1"),
        ("drive:Ab:item-1", "drive:Ab:item-1"),           # an uppercase head is no tag
        ("drive:abcdefg:item-1", "drive:abcdefg:item-1"),   # seven characters is no tag
        ("drive:a1:", "drive:a1:"),                         # a tag with no item id
        ("drive:1a:item-1", "drive:1a:item-1"),             # a tag starts with a letter
    ])
    def test_parse_untagged(self, text, raw):
        got = parse(text)
        assert got == UntaggedRef(raw=raw)
        assert str(got) == raw
        assert is_drive_ref(text)

    def test_untagged_is_not_a_tagged_ref(self):
        assert not isinstance(parse("drive:01ABCDEF2GHIJ"), DriveRef)
