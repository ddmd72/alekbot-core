"""A7: one file's adjacent chunks merge into one run; the overlap goes by offsets, never by guessing."""
from src.domain.drive_excerpt_chunk import ExcerptChunk
from src.domain.drive_excerpts import GAP_JOINT, merge_runs
from tests.unit.domain.drive_excerpt_builders import chunk, chunks_of

PROSE = " ".join(f"Step {i} of Tom's plan goes to the review at Acme before the next one starts." for i in range(90))
TABLE = ("Acme budget lines for the R&D review.\n\n| Item | Amount |\n| --- | --- |\n"
         + "\n".join(f"| Line {i} | {i * 10} EUR |" for i in range(220)))


def test_prose_overlap_is_removed_and_the_source_comes_back_whole():
    chunks = chunks_of(PROSE)
    assert len(chunks) >= 3 and chunks[1].char_start < chunks[0].char_end   # the chunker overlapped them
    runs = merge_runs(chunks)
    assert len(runs) == 1
    assert (runs[0].first, runs[0].last) == (1, len(chunks))
    assert runs[0].text == PROSE


def test_a_repeated_table_header_is_kept_once_at_the_head():
    chunks = chunks_of(TABLE)
    assert len(chunks) >= 3 and chunks[1].text.startswith("| Item | Amount |")
    runs = merge_runs(chunks[1:3])
    assert runs[0].text.count("| Item | Amount |") == 1
    assert runs[0].text.startswith("| Item | Amount |\n| --- | --- |\n")
    whole = merge_runs(chunks)[0].text
    assert whole == TABLE


def test_non_adjacent_chunks_are_separate_runs_in_index_order():
    a, b, c = chunk(index=4, text="Fourth part."), chunk(index=0, text="First part."), chunk(index=1, text="Second.")
    runs = merge_runs([a, b, c])
    assert [(r.first, r.last) for r in runs] == [(1, 2), (5, 5)]


def test_disagreeing_offsets_are_joined_never_cut():
    # the second chunk claims to start 10 characters back, but its text does not repeat the first one's tail
    first = ExcerptChunk("item-1", 1, 0, "The review process starts here", 0, 30)
    second = ExcerptChunk("item-1", 1, 1, "afterwards Tom signs the plan.", 20, 50)
    runs = merge_runs([first, second])
    assert runs[0].text == "The review process starts here" + GAP_JOINT + "afterwards Tom signs the plan."


def test_a_text_shorter_than_its_span_is_never_cut_inside_a_word():
    first = ExcerptChunk("item-1", 1, 0, "The review process starts here", 0, 30)
    second = ExcerptChunk("item-1", 1, 1, "here today", 26, 60)          # span 34, text 10: offsets are wrong
    text = merge_runs([first, second])[0].text
    assert text == "The review process starts here" + GAP_JOINT + "here today"


def test_a_gap_between_spans_becomes_a_line_break():
    first = chunk(index=0, text="| Line 1 | 10 EUR |", start=0)
    second = chunk(index=1, text="| Line 2 | 20 EUR |", start=len(first.text) + 1)
    assert merge_runs([first, second])[0].text == "| Line 1 | 10 EUR |\n| Line 2 | 20 EUR |"


def test_two_files_stay_apart():
    runs = merge_runs([chunk("item-1", 0, "Tom's plan."), chunk("item-2", 1, "Lease terms.")])
    assert [(r.item_id, r.first, r.last) for r in runs] == [("item-1", 1, 1), ("item-2", 2, 2)]
