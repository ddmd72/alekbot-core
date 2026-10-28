"""The passages tools' arguments (USER_DRIVE_INDEXING_RFC §28.3 A3, A5, A7): parsed or refused with a correction."""
import pytest

from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_request import (
    ExcerptRequest, excerpt_request, parse_file_refs, parse_range, range_request,
)


class TestFileRefs:
    def test_a_list_and_a_comma_string_give_the_same_refs(self):
        assert parse_file_refs(["drive:abc", " drive:def "]) == ("drive:abc", "drive:def")
        assert parse_file_refs("drive:abc, drive:def") == ("drive:abc", "drive:def")

    def test_empty_means_not_given(self):
        assert parse_file_refs(None) == ()
        assert parse_file_refs("") == ()
        assert parse_file_refs([]) == ()
        assert parse_file_refs("drive:abc, ,") == ("drive:abc",)

    def test_a_ref_given_twice_counts_once(self):
        assert parse_file_refs(["drive:abc", "drive:abc"]) == ("drive:abc",)

    def test_ten_refs_pass_eleven_are_refused(self):
        assert len(parse_file_refs([f"drive:item-{i}" for i in range(10)])) == 10
        with pytest.raises(ValueError, match="At most 10 files"):
            parse_file_refs([f"drive:item-{i}" for i in range(11)])

    def test_a_non_ref_is_refused_by_name(self):
        with pytest.raises(ValueError, match="'R&D/Tom's plan.docx' is not a drive: ref"):
            parse_file_refs(["drive:abc", "R&D/Tom's plan.docx"])


class TestRange:
    @pytest.mark.parametrize("value, expected", [
        ("3", (3, 3)), (3, (3, 3)), ("3-5", (3, 5)), ("3–5", (3, 5)), (" 3 - 5 ", (3, 5)), ("1-10", (1, 10)),
    ])
    def test_the_forms_a_model_writes(self, value, expected):
        assert parse_range(value) == expected

    def test_a_reversed_range_is_refused(self):
        with pytest.raises(ValueError, match="ends before it starts"):
            parse_range("5-3")

    def test_more_than_ten_passages_are_refused(self):
        with pytest.raises(ValueError, match="At most 10 passages"):
            parse_range("1-11")

    @pytest.mark.parametrize("value", ["0", "0-2", "three", "3,5", "-3"])
    def test_what_is_not_a_display_number_is_refused(self, value):
        with pytest.raises(ValueError, match="not a passage number"):
            parse_range(value)

    @pytest.mark.parametrize("value", ["", None, "  "])
    def test_an_empty_range_is_required(self, value):
        with pytest.raises(ValueError, match="passages is required"):
            parse_range(value)


class TestRequests:
    def test_a_find_keeps_its_question_folder_and_refs(self):
        req = excerpt_request("  How does the  review process work? ", " R&D ", "drive:abc")
        assert req == ExcerptRequest(question="How does the review process work?", folder="R&D",
                                     file_refs=("drive:abc",))

    @pytest.mark.parametrize("question", ["", "   ", None])
    def test_an_empty_question_is_refused(self, question):
        with pytest.raises(ValueError, match="question is required"):
            excerpt_request(question, "R&D")

    def test_a_range_request_holds_display_numbers(self):
        assert range_request("drive:abc", "4-6") == ExcerptRangeRequest(file_ref="drive:abc", first=4, last=6)

    @pytest.mark.parametrize("ref", ["", None, " "])
    def test_an_empty_ref_is_refused(self, ref):
        with pytest.raises(ValueError, match="file_ref is required"):
            range_request(ref, "1")

    def test_a_range_on_a_non_ref_is_refused(self):
        with pytest.raises(ValueError, match="is not a drive: ref"):
            range_request("Tom's plan", "1")
