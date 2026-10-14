"""The window budget (USER_DRIVE_INDEXING_RFC §10.2; WINDOW_CHARS_TOTAL): one result set's windows never exceed it,
every content hit keeps its best chunk (trimmed around its query term when it must be), and neighbours go to better
hits first. `shortened` marks a trimmed best chunk only, never a neighbour left out (final review F5-1)."""
from src.domain.drive_content_search import WindowRequest, budget_windows
from src.domain.drive_content_terms import content_terms
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import CHUNK_CHARS, K, WINDOW_CHARS_TOTAL


def doc(item, size, marker=None, n=3, anchor=1):
    """`n` contiguous chunks of `size` characters (no overlap); the anchor starts with `marker`."""
    chunks = []
    for i in range(n):
        head = (marker or f"{item}-best") if i == anchor else f"{item}-c{i}"
        text = (head + " " + "x" * size)[:size]
        chunks.append(DriveChunk("u", item, 1, i, text, i * size, (i + 1) * size, "pdf", content_terms(text)))
    return WindowRequest(anchor, chunks)


def test_eight_hits_of_three_full_chunks_stay_within_the_budget_and_keep_every_best_chunk():
    requests = [doc(f"h{n}", CHUNK_CHARS) for n in range(K)]
    windows = budget_windows(requests, (), WINDOW_CHARS_TOTAL)
    assert sum(len(w.text) for w in windows) <= WINDOW_CHARS_TOTAL
    for n, window in enumerate(windows):
        assert window.text.startswith(f"h{n}-best") and window.shortened
        assert f"h{n}-c0" not in window.text and f"h{n}-c2" not in window.text


def test_neighbours_go_in_rank_order_while_the_budget_lasts():
    requests = [doc(f"h{n}", 2_000) for n in range(3)]   # best chunks 6 000, 6 000 left for neighbours
    windows = budget_windows(requests, (), 12_000)
    assert [w.shortened for w in windows] == [False, False, False]   # no best chunk was trimmed (F5-1)
    assert all(f"h{n}-best" in w.text for n, w in enumerate(windows))
    assert "h0-c0" in windows[0].text and "h0-c2" in windows[0].text
    assert "h1-c0" in windows[1].text and "h1-c2" not in windows[1].text   # the last 2 000 went to h1's first one
    assert windows[2].text.startswith("h2-best") and "h2-c" not in windows[2].text
    assert sum(len(w.text) for w in windows) == 12_000


def test_a_lower_hit_never_gets_a_neighbour_a_better_one_was_denied():
    requests = [doc("big", 2_000), doc("small", 300)]   # 2 300 for best chunks; 1 700 left: big's 2 000 does not fit
    windows = budget_windows(requests, (), 4_000)
    assert [w.shortened for w in windows] == [False, False]   # both best chunks whole: no trim (F5-1)
    assert windows[0].text.startswith("big-best") and len(windows[0].text) == 2_000
    assert "big-c" not in windows[0].text                     # big's neighbour did not fit
    assert windows[1].text.startswith("small-best") and len(windows[1].text) == 300
    assert "small-c" not in windows[1].text                   # so small gets none, though its 300 would fit


def test_a_trimmed_best_chunk_keeps_the_query_term():
    filler = "Lorem ipsum dolor sit amet. " * 80
    text = (filler + " Factura INV-2024/0012 total 1 250,00 EUR. ")[:2_400]
    best = DriveChunk("u", "inv", 1, 0, text, 0, len(text), "pdf", content_terms(text))
    others = [doc(f"h{n}", CHUNK_CHARS) for n in range(7)]
    windows = budget_windows([WindowRequest(0, [best])] + others, ("inv20240012", "20240012"), WINDOW_CHARS_TOTAL)
    assert "INV-2024/0012" in windows[0].text and windows[0].shortened
    assert windows[0].text.startswith("…") and len(windows[0].text) <= WINDOW_CHARS_TOTAL // 8


def test_a_short_result_set_is_not_shortened():
    windows = budget_windows([doc("a", 500)], (), WINDOW_CHARS_TOTAL)
    assert not windows[0].shortened and "a-c0" in windows[0].text and "a-c2" in windows[0].text


def test_a_hit_whose_best_chunk_is_gone_gets_no_window_and_no_share():
    gone = WindowRequest(5, [])
    windows = budget_windows([gone, doc("a", 2_000)], (), 3_000)
    assert windows[0].text == "" and not windows[0].shortened
    assert windows[1].text.startswith("a-best") and len(windows[1].text) == 2_000   # the whole budget was its own
    assert "a-c" not in windows[1].text                                             # a 2 000 neighbour did not fit
    assert not windows[1].shortened                       # its best chunk is whole: a dropped neighbour is no trim (F5-1)
