"""G9 (GOOGLE_DRIVE_RFC §5, design §1.2): one content transfer may take 300 s, and the tick admits a file only when
its worst case, summed from the same constants, still ends before the deadline."""
from src.domain.drive_index_params import (
    DOWNLOAD_TIMEOUT_S, EMBED_FILE_BUDGET_S, EXTRACT_WALL_TIMEOUT_S, FILE_START_MARGIN_S, MODEL_FILE_BUDGET_S,
    TICK_DEADLINE_S,
)
from src.domain.drive_index_planner import can_start_file

ROUTED_WORST = (DOWNLOAD_TIMEOUT_S + EXTRACT_WALL_TIMEOUT_S + MODEL_FILE_BUDGET_S + EMBED_FILE_BUDGET_S
                + FILE_START_MARGIN_S)


def test_the_download_timeout_is_300_s():
    assert DOWNLOAD_TIMEOUT_S == 300


def test_a_routed_file_is_refused_at_its_worst_case_and_admitted_one_second_earlier():
    assert not can_start_file(TICK_DEADLINE_S - ROUTED_WORST, routed=True)
    assert can_start_file(TICK_DEADLINE_S - ROUTED_WORST - 1, routed=True)
