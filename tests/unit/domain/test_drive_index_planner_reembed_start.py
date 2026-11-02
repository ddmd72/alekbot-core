from src.domain.drive_index_params import DOWNLOAD_TIMEOUT_S, EMBED_FILE_BUDGET_S, EXTRACT_WALL_TIMEOUT_S, FILE_START_MARGIN_S, TICK_DEADLINE_S
from src.domain.drive_index_planner import can_start_file, can_start_reembed
from src.services.drive_indexing_service import _Budget

REEMBED_WORST = 2 * EMBED_FILE_BUDGET_S + FILE_START_MARGIN_S


def test_a_reembed_fits_at_an_early_start():
    assert can_start_reembed(0.0)


def test_a_reembed_counts_both_embedding_waits():
    assert can_start_reembed(TICK_DEADLINE_S - REEMBED_WORST - 0.5)
    assert not can_start_reembed(TICK_DEADLINE_S - REEMBED_WORST)


def test_a_start_where_a_reembed_fits_but_a_parsed_file_does_not():
    """A parsed file's worst case (a 300 s transfer, GOOGLE_DRIVE_RFC G9) is longer than the re-embed's two waits,
    which download nothing: each gate keeps its own bound."""
    parsed_worst = DOWNLOAD_TIMEOUT_S + EXTRACT_WALL_TIMEOUT_S + EMBED_FILE_BUDGET_S + FILE_START_MARGIN_S
    at = TICK_DEADLINE_S - REEMBED_WORST - 0.5
    assert parsed_worst > REEMBED_WORST
    assert can_start_reembed(at) and not can_start_file(at, routed=False)


def test_the_budget_gate_uses_the_reembed_bound():
    now = [0.0]
    budget = _Budget(started=0.0, monotonic=lambda: now[0])
    assert budget.reembed_allowed()
    budget.files = 0
    assert not budget.reembed_allowed()
