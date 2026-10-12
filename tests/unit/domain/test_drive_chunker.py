"""Chunker (USER_DRIVE_INDEXING_RFC §8.4, §8.3, §5.4). Pure; properties over seeded random texts (RFC Δ8)."""
import random

import pytest

from src.domain.drive_chunker import chunk_text
from src.domain.drive_index_params import CHUNK_CHARS, CHUNK_OVERLAP, MAX_CHUNKS_PER_FILE

SEEDS = range(200)
_WORDS = ("contract", "payment", "party", "shall", "within", "days", "invoice", "оплата", "сторона", "договір",
          "rent", "deposit", "tenant", "the", "of", "and", "a", "to", "in", "is")


def sentence(rng, words=None):
    n = words or rng.randint(3, 40)
    return " ".join(rng.choice(_WORDS) for _ in range(n)).capitalize() + rng.choice([".", "!", "?", "…"])


def table(rng, rows):
    head = "| item | amount | date |\n|---|---|---|"
    body = "\n".join(f"| {rng.choice(_WORDS)} {i} | {rng.randint(1, 99999)}.{rng.randint(0, 99):02d} | 2026-0{i % 9 + 1}-1{i % 9} |"
                     for i in range(rows))
    return head + "\n" + body


def random_doc(rng):
    blocks = []
    for _ in range(rng.randint(1, 30)):
        r = rng.random()
        if r < 0.6:
            blocks.append(" ".join(sentence(rng) for _ in range(rng.randint(1, 25))))
        elif r < 0.7:
            blocks.append("\n".join(sentence(rng) for _ in range(rng.randint(2, 30))))   # one line per sentence
        elif r < 0.85:
            blocks.append(table(rng, rng.randint(1, 200)))
        elif r < 0.95:
            blocks.append("x" * rng.randint(1, 7000))   # no boundary at all: hard cuts
        else:
            blocks.append(" ".join("y" * rng.randint(1, 300) for _ in range(rng.randint(1, 40))))
    return ("\n\n" + " " * rng.randint(0, 2) + "\n").join(blocks) if rng.random() < 0.3 else "\n\n".join(blocks)


def _headed(chunk, source):
    return chunk.text != source[chunk.char_start:chunk.char_end]


class TestEmpty:
    @pytest.mark.parametrize("text", ["", "   ", "\n\n\t\n"])
    def test_no_chunks(self, text):
        out = chunk_text(text)
        assert out.chunks == () and not out.truncated


class TestSmall:
    def test_one_chunk_is_the_stripped_text(self):
        text = "\n  Clause one. Clause two.\n\nSecond paragraph.  \n"
        (chunk,), truncated = chunk_text(text).chunks, chunk_text(text).truncated
        assert chunk.text == "Clause one. Clause two.\n\nSecond paragraph." and not truncated
        assert text[chunk.char_start:chunk.char_end] == chunk.text


class TestBoundaries:
    def test_ends_at_a_paragraph_when_that_keeps_it_half_full(self):
        rng = random.Random(7)
        paragraphs = [" ".join(sentence(rng, 12) for _ in range(9)) for _ in range(8)]   # ~800 chars each
        text = "\n\n".join(paragraphs)
        chunks = chunk_text(text).chunks
        assert len(chunks) > 2
        for c in chunks[:-1]:
            assert text[c.char_end:c.char_end + 2] == "\n\n"

    def test_falls_back_to_sentences(self):
        rng = random.Random(3)
        text = " ".join(sentence(rng, 10) for _ in range(200))   # one paragraph, ~13k chars
        chunks = chunk_text(text).chunks
        for c in chunks[:-1]:
            assert c.text.rstrip()[-1] in ".!?…"

    def test_hard_cut_without_any_boundary(self):
        text = "z" * (CHUNK_CHARS * 3 + 5)
        chunks = chunk_text(text).chunks
        assert [len(c.text) for c in chunks] == [CHUNK_CHARS, CHUNK_CHARS, CHUNK_CHARS, 5]

    def test_word_boundary_before_a_hard_cut(self):
        text = ("w" * 1500 + " ") * 4
        assert all(not c.text.startswith(" ") and not c.text.endswith(" ") for c in chunk_text(text).chunks)


class TestOverlap:
    def test_next_chunk_repeats_trailing_sentences(self):
        rng = random.Random(11)
        text = " ".join(sentence(rng, 8) for _ in range(300))
        chunks = chunk_text(text).chunks
        for a, b in zip(chunks, chunks[1:]):
            overlap = a.char_end - b.char_start
            assert 0 < overlap <= CHUNK_OVERLAP
            assert text[b.char_start:a.char_end] in a.text

    def test_a_long_last_sentence_is_not_repeated(self):
        text = ("word " * 120).strip() + ". " + ("long " * 300).strip() + ". " + ("tail " * 400).strip() + "."
        chunks = chunk_text(text).chunks
        assert all(b.char_start >= a.char_end for a, b in zip(chunks, chunks[1:]))


class TestTables:
    def test_continued_table_chunks_repeat_the_header(self):
        rng = random.Random(5)
        text = "Intro paragraph.\n\n" + table(rng, 400) + "\n\nAfter the table."
        chunks = chunk_text(text).chunks
        header = "| item | amount | date |\n|---|---|---|"
        continued = [c for c in chunks if _headed(c, text)]
        assert len(continued) >= 3
        for c in continued:
            assert c.text == header + "\n" + text[c.char_start:c.char_end]
            assert len(c.text) <= CHUNK_CHARS

    def test_table_rows_are_never_overlapped(self):
        rng = random.Random(6)
        text = table(rng, 500)
        chunks = chunk_text(text).chunks
        assert all(b.char_start > a.char_end for a, b in zip(chunks, chunks[1:]))

    def test_a_wide_header_is_not_repeated(self):
        wide = "| " + " | ".join(f"column {i}" for i in range(200)) + " |"
        sep = "|" + "---|" * 200
        rows = "\n".join("| " + " | ".join(str(i) for _ in range(5)) + " |" for i in range(600))
        text = f"{wide}\n{sep}\n{rows}"
        assert not any(_headed(c, text) for c in chunk_text(text).chunks)


class TestCap:
    def test_truncated_past_max_chunks(self):
        text = "\n\n".join("p" * 2000 for _ in range(10))
        out = chunk_text(text, max_chunks=4)
        assert len(out.chunks) == 4 and out.truncated

    def test_exact_fit_is_not_truncated(self):
        text = "\n\n".join("p" * 2000 for _ in range(4))
        out = chunk_text(text, max_chunks=4)
        assert len(out.chunks) == 4 and not out.truncated

    def test_default_cap_is_the_file_cap(self):
        out = chunk_text("q" * (CHUNK_CHARS * (MAX_CHUNKS_PER_FILE + 3)))
        assert len(out.chunks) == MAX_CHUNKS_PER_FILE and out.truncated


@pytest.mark.parametrize("seed", SEEDS)
def test_properties(seed):
    rng = random.Random(seed)
    text = random_doc(rng)
    out = chunk_text(text)
    assert out == chunk_text(text)   # deterministic
    covered = [False] * len(text)
    for c in out.chunks:
        assert 0 < len(c.text) <= CHUNK_CHARS
        assert 0 <= c.char_start < c.char_end <= len(text)
        body = text[c.char_start:c.char_end]
        assert c.text == body or c.text.endswith("\n" + body)   # a continued table: its header, then the rows
        for i in range(c.char_start, c.char_end):
            covered[i] = True
    for a, b in zip(out.chunks, out.chunks[1:]):
        assert b.char_start > a.char_start and b.char_end > a.char_end   # forward progress
        assert a.char_end - b.char_start <= CHUNK_OVERLAP
    if not out.truncated:
        assert all(covered[i] for i, ch in enumerate(text) if not ch.isspace())
