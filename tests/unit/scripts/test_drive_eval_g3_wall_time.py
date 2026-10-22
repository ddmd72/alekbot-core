"""G3 records each case's wall time and the run's median (DRIVE_SPECIALIST_RFC §12: the drive-turn latency bar is
p50 after ≤ p50 before + 25 %). It lands before the step-0 'before' run, so both runs measure the same thing."""
from scripts.validation.drive_eval.smart_dry_run import wall_p50


def test_the_median_of_an_odd_and_an_even_number_of_runs():
    assert wall_p50([{"wall_s": 3.0}, {"wall_s": 1.0}, {"wall_s": 2.0}]) == 2.0
    assert wall_p50([{"wall_s": 4.0}, {"wall_s": 1.0}, {"wall_s": 2.0}, {"wall_s": 3.0}]) == 2.5


def test_rows_without_a_wall_time_are_left_out_and_none_gives_zero():
    assert wall_p50([{"id": "a"}, {"wall_s": 5.0}]) == 5.0
    assert wall_p50([]) == 0.0
