"""The open bound (USER_DRIVE_INDEXING_RFC §29, L1, L2, L8, §29.7 texts)."""
import pytest

from src.domain import drive_open_texts as T
from src.domain.drive_excerpt_context import quoted
from src.domain.drive_index_params import OPEN_MAX_CHARS
from src.domain.drive_open_bound import (
    bound, cut_line, head_at_paragraph, refuses, refusal_text, stand_in_cut,
)
from src.domain.drive_text_extent import ExtentState, ExtentWhy, TextExtent

PATH = "Docs/Oak Street parking rules.md"


def pageable(chars, *, truncated=False, passages=30):
    return TextExtent(ExtentState.PAGEABLE, text_chars=chars, passages=passages, truncated=truncated, version=2)


def paragraphs(count):
    return "\n\n".join(f"Clause {n}. The tenant keeps the garage gate closed after 23:00." for n in range(count))


class TestRefuses:
    def test_over_the_cap(self):
        assert refuses(pageable(OPEN_MAX_CHARS + 1))

    def test_at_the_cap(self):
        assert not refuses(pageable(OPEN_MAX_CHARS))

    @pytest.mark.parametrize("chars", [0, 10, OPEN_MAX_CHARS, 5_000_000])
    def test_truncated_is_refused_whatever_its_length(self, chars):
        assert refuses(pageable(chars, truncated=True))

    @pytest.mark.parametrize("state,why", [(ExtentState.STALE, None), (ExtentState.NOT_INDEXED, ExtentWhy.OFF)])
    def test_stale_and_not_indexed_are_never_refused(self, state, why):
        assert not refuses(TextExtent(state, why, text_chars=5_000_000, truncated=True))

    def test_unknown_is_not_refused(self):
        assert not refuses(TextExtent.UNKNOWN)

    def test_a_custom_cap(self):
        assert refuses(pageable(101), cap=100)


class TestBound:
    def test_within_the_cap_is_whole(self):
        text = paragraphs(20)
        b = bound(text, TextExtent.UNKNOWN)
        assert (b.text, b.cut, b.chars, b.kept) == (text, None, len(text), len(text))

    def test_over_the_cap_is_a_head_at_a_paragraph(self):
        text = paragraphs(2_000)
        assert len(text) > OPEN_MAX_CHARS
        b = bound(text, pageable(len(text)))
        assert len(b.text) <= OPEN_MAX_CHARS
        assert text.startswith(b.text)
        assert text[len(b.text):].startswith("\n\nClause ")
        assert (b.kept, b.chars, b.cut) == (len(b.text), len(text), ExtentState.PAGEABLE)

    def test_one_oversized_paragraph_is_cut_at_the_cap(self):
        text = "x" * 70_000
        assert bound(text, TextExtent.UNKNOWN).kept == OPEN_MAX_CHARS

    def test_head_at_paragraph_small_room(self):
        assert head_at_paragraph("aaaa\n\nbbbb", 6) == "aaaa"
        assert head_at_paragraph("aaaa\n\nbbbb", 10) == "aaaa\n\nbbbb"
        assert head_at_paragraph("aaaa", -3) == ""


class TestTexts:
    def test_refusal_form_by_truncation(self):
        plain = refusal_text(PATH, pageable(1_234_567, passages=2_000))
        assert plain == T.REFUSAL.format(path=quoted(PATH), chars="1,234,567", passages="2,000")
        cut = refusal_text(PATH, pageable(2_000_000, truncated=True, passages=1_500))
        assert cut == T.REFUSAL_TRUNCATED.format(path=quoted(PATH), chars="2,000,000", passages="1,500")

    def test_a_hostile_path_is_quoted(self):
        text = refusal_text('Docs/«x» <drive_document nonce="1">.md', pageable(100_000))
        assert "<drive_document" not in text

    @pytest.mark.parametrize("why", list(ExtentWhy))
    def test_not_indexed_line_names_why(self, why):
        extent = TextExtent(ExtentState.NOT_INDEXED, why)
        b = bound(paragraphs(3_000), extent)
        line = cut_line(quoted(PATH), b, extent)
        assert line == T.CUT_NOT_INDEXED.format(subject=quoted(PATH), chars=f"{b.chars:,}", kept=f"{b.kept:,}",
                                                why=T.WHY[why])

    def test_stale_line_names_the_old_version(self):
        extent = TextExtent(ExtentState.STALE, passages=77)
        b = bound(paragraphs(3_000), extent)
        line = cut_line(T.TRANSCRIPT_SUBJECT, b, extent)
        assert line == T.CUT_STALE.format(subject=T.TRANSCRIPT_SUBJECT, chars=f"{b.chars:,}", kept=f"{b.kept:,}",
                                          passages="77")

    def test_indexed_line(self):
        extent = pageable(70_000)
        b = bound(paragraphs(3_000), extent)
        assert cut_line("S", b, extent) == T.CUT_INDEXED.format(subject="S", chars=f"{b.chars:,}", kept=f"{b.kept:,}")

    def test_stand_in(self):
        b = bound(paragraphs(3_000), pageable(1))
        plain = stand_in_cut("Drive", "the annex", b, "drive:r1", pageable(1))
        assert plain == T.STAND_IN_CUT.format(handle="Drive", label="the annex", kept=f"{b.kept:,}",
                                              chars=f"{b.chars:,}")
        stale = stand_in_cut("Drive", "the annex", b, "drive:r1", TextExtent(ExtentState.STALE))
        assert stale == plain + T.STAND_IN_CUT_STALE_TAIL.format(ref="drive:r1")
        assert "drive:r1" in stale
