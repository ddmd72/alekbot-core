"""The search window (drive_content_search.window_text) over the tick's real chunker (drive_chunker, §8.4, §10.2):
adjacent chunks join back into the source text — overlap cut, no false gap marker where the chunker left only
whitespace between spans, and a continued table's repeated header not repeated inside the window."""
import re

from src.domain.drive_chunker import chunk_text
from src.domain.drive_content_search import window_text
from src.domain.drive_index import DriveChunk

_PROSE = " ".join(f"Sentence number {i} talks about the lease of flat {i % 7} in Porto." for i in range(120))
_TABLE = "| month | amount |\n|---|---|\n" + "\n".join(f"| 2026-{i % 12 + 1:02d} row {i} | {i * 10} EUR |"
                                                       for i in range(160))
_SOURCE = f"{_PROSE}\n\n{_TABLE}\n\nAfter the table: the deposit is 1200 EUR. {_PROSE}"


def _chunks():
    out = chunk_text(_SOURCE)
    assert not out.truncated and len(out.chunks) > 4
    return [DriveChunk("u", "i", 1, n, c.text, c.char_start, c.char_end, "pdf") for n, c in enumerate(out.chunks)]


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_all_chunks_join_back_into_the_source():
    chunks = _chunks()
    joined = window_text(chunks)
    assert "[…]" not in joined
    assert _squash(joined) == _squash(_SOURCE)
    assert joined.count("| month | amount |") == 1


def test_every_three_chunk_window_is_a_contiguous_piece_of_the_source():
    chunks = _chunks()
    for i in range(1, len(chunks) - 1):
        window = window_text(chunks[i - 1:i + 2])
        assert "[…]" not in window
        first = chunks[i - 1]
        header = len(first.text) - (first.char_end - first.char_start)   # the first chunk keeps its own header
        assert _squash(window[header:]) == _squash(_SOURCE[first.char_start:chunks[i + 1].char_end]), i


def test_a_missing_neighbour_still_shows_the_gap():
    chunks = _chunks()
    assert "[…]" in window_text([chunks[0], chunks[2]])
