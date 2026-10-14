"""The window budget with best chunks of different sizes (USER_DRIVE_INDEXING_RFC §10.2, review D minor): a best chunk
is trimmed only when the best chunks together exceed the budget, and then only down to the share the shorter ones
leave free — never to an equal share while the budget has room."""
from src.domain.drive_content_search import budget_windows
from src.domain.drive_index_params import CHUNK_CHARS, WINDOW_CHARS_TOTAL

from tests.unit.domain.test_drive_content_search_window_budget import doc


def test_one_full_chunk_and_seven_short_ones_are_not_trimmed():
    requests = [doc("big", CHUNK_CHARS, n=1, anchor=0)] + [doc(f"s{n}", 100, n=1, anchor=0) for n in range(7)]
    windows = budget_windows(requests, (), WINDOW_CHARS_TOTAL)
    assert len(windows[0].text) == CHUNK_CHARS and not windows[0].shortened
    assert all(len(w.text) == 100 and not w.shortened for w in windows[1:])


def test_an_oversized_best_chunk_keeps_what_the_short_ones_leave():
    requests = [doc("big", 11_000, n=1, anchor=0)] + [doc(f"s{n}", 500, n=1, anchor=0) for n in range(7)]
    windows = budget_windows(requests, (), 12_000)
    assert windows[0].shortened and len(windows[0].text) == 12_000 - 7 * 500
    assert all(len(w.text) == 500 and not w.shortened for w in windows[1:])
    assert sum(len(w.text) for w in windows) <= 12_000
