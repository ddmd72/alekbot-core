"""`shortened` marks a trimmed best chunk only (USER_DRIVE_INDEXING_RFC §10.2, final review F5-1).

Smart opens a file before quoting a shortened passage. A window whose best chunk is whole and only lost a neighbour to
the budget still shows the whole matching passage: marking it shortened sent Smart to open every file once four or more
content hits shared WINDOW_CHARS_TOTAL."""
from src.domain.drive_content_search import WindowRequest, budget_windows
from src.domain.drive_content_terms import content_terms
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import CHUNK_CHARS, WINDOW_CHARS_TOTAL


def _doc(item, size, n=3, anchor=1):
    chunks = []
    for i in range(n):
        text = ((f"{item}-best" if i == anchor else f"{item}-c{i}") + " " + "x" * size)[:size]
        chunks.append(DriveChunk("u", item, 1, i, text, i * size, (i + 1) * size, "pdf", content_terms(text)))
    return WindowRequest(anchor, chunks)


def test_a_whole_best_chunk_whose_neighbours_were_dropped_is_not_shortened():
    requests = [_doc(f"h{n}", CHUNK_CHARS) for n in range(4)]   # 4 x 2 400 best chunks; neighbours do not all fit
    windows = budget_windows(requests, (), WINDOW_CHARS_TOTAL)
    assert all(f"h{n}-best" in w.text for n, w in enumerate(windows))
    assert any("-c" not in w.text for w in windows)          # some lost their neighbours to the budget
    assert [w.shortened for w in windows] == [False] * 4


def test_a_trimmed_best_chunk_is_shortened():
    requests = [_doc(f"h{n}", CHUNK_CHARS) for n in range(8)]   # 8 x 2 400 > 12 000: every best chunk is trimmed
    windows = budget_windows(requests, (), WINDOW_CHARS_TOTAL)
    assert all(w.shortened for w in windows)
