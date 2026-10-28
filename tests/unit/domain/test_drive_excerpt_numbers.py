"""One numbering: stored chunk indexes are 0-based, every number a model sees or gives is index + 1 (§28.3 A10)."""
import pytest

from src.domain.drive_excerpt_chunk import display_number, stored_index
from src.domain.drive_excerpt_render import render_excerpts
from src.domain.drive_excerpt_request import range_request
from src.domain.drive_excerpts import merge_runs
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result


@pytest.mark.parametrize("index", [0, 1, 9, 399])
def test_display_and_stored_are_inverse(index):
    assert display_number(index) == index + 1
    assert stored_index(display_number(index)) == index


def test_there_is_no_passage_zero():
    with pytest.raises(ValueError):
        stored_index(0)


def test_a_run_holds_display_numbers():
    run = merge_runs([chunk(index=0, text="A."), chunk(index=1, text="B.", start=2)])[0]
    assert (run.first, run.last) == (1, 2)


def test_the_head_line_continue_at_and_a_range_request_agree():
    # a file of 12 chunks; the reader was shown stored chunks 4-5 and told to continue at the next
    shown = excerpt_file(chunks=[chunk(index=4, text="Fifth."), chunk(index=5, text="Sixth.", start=4007)])
    text = render_excerpts(result(shown, continue_at=display_number(6)), nonce=nonces()).text
    head = next(line for line in text.splitlines() if line.startswith("[id "))
    assert head.startswith("[id drive:item-1:5-6 · ") and " · passages 5–6 of 12" in head
    assert "from passage 7 continues" in text
    # what the reader gives back points at the stored chunks the service reads next
    req = range_request("drive:item-1", "7-9")
    assert [stored_index(n) for n in range(req.first, req.last + 1)] == [6, 7, 8]
