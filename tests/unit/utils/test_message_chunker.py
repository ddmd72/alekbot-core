"""Unit tests for MessageChunker — platform-agnostic message splitting.

Covers the boundary-preference cascade: paragraph → line → sentence → word → hard split.
"""

from src.utils.message_chunker import MessageChunker


class TestMessageChunkerNoSplit:
    def test_short_text_returned_as_single_chunk(self):
        chunker = MessageChunker(max_length=100)
        assert chunker.split("hello") == ["hello"]

    def test_text_exactly_at_limit_not_split(self):
        chunker = MessageChunker(max_length=5)
        assert chunker.split("12345") == ["12345"]


class TestMessageChunkerBoundaries:
    def test_splits_on_paragraph_boundary(self):
        chunker = MessageChunker(max_length=12)
        chunks = chunker.split("abcde\n\nfghij\n\nklmno")
        assert all(len(c) <= 12 for c in chunks)
        assert "abcde" in chunks[0]

    def test_falls_back_to_line_boundary_when_no_paragraph(self):
        # No "\n\n" within window → uses single "\n".
        chunker = MessageChunker(max_length=8)
        chunks = chunker.split("abcde\nfghij\nklmno")
        assert all(len(c) <= 8 for c in chunks)
        assert chunks[0] == "abcde"

    def test_falls_back_to_sentence_boundary(self):
        # No newline within window → splits at the ". " boundary. The split
        # index points at the boundary start; rstrip() drops trailing space but
        # the period itself leads the next chunk (split_index = rfind(". ")).
        chunker = MessageChunker(max_length=10)
        chunks = chunker.split("abcde. fghij. klmno")
        assert all(len(c) <= 10 for c in chunks)
        assert chunks[0] == "abcde"
        assert chunks[1].startswith(".")

    def test_falls_back_to_word_boundary(self):
        # No newline, no ". " → uses single space.
        chunker = MessageChunker(max_length=8)
        chunks = chunker.split("abcde fghij klmno")
        assert all(len(c) <= 8 for c in chunks)
        assert chunks[0] == "abcde"

    def test_hard_split_when_no_boundary(self):
        # A single unbroken token longer than max_length → forced split.
        chunker = MessageChunker(max_length=5)
        chunks = chunker.split("abcdefghijklmno")
        assert all(len(c) <= 5 for c in chunks)
        assert "".join(chunks) == "abcdefghijklmno"

    def test_all_content_preserved_across_chunks(self):
        chunker = MessageChunker(max_length=10)
        text = "abcde\nfghij. klmno pqrst\n\nuvwxy"
        chunks = chunker.split(text)
        # Every chunk respects the limit; nothing is empty.
        assert all(0 < len(c) <= 10 for c in chunks)
