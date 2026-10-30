"""T-1, part C's share (MULTI_DRIVE_RFC §13): the index, transcript and mirror modules of `src/domain` hold no string
constant (docstrings included) that names a provider or a provider shape. Their readers moved to the typed media facts,
the content identity and the drive identity in slice 1b. The scan is the frozen core's (`test_multi_drive_no_provider_strings`)."""
from pathlib import Path

import pytest

from tests.unit.domain.test_multi_drive_no_provider_strings import _hits

ROOT = Path(__file__).resolve().parents[3]
PART_C_MODULES = tuple(f"src/domain/{name}.py" for name in (
    "drive_card", "drive_capture", "drive_index_run", "drive_index_policy", "drive_index_planner", "drive_enrich_plan",
    "drive_transcript", "drive_transcript_job", "drive_transcript_plan", "drive_transcript_render",
    "drive_transcript_stitch", "drive_transcript_time", "drive_transcript_windows", "drive_mirror", "drive_mirror_plan",
    "drive_mirror_render", "drive_mirror_names", "drive_index", "drive_index_lean", "drive_usage", "drive_task_keys",
    "drive_index_params",
))


@pytest.mark.parametrize("module", PART_C_MODULES)
def test_no_provider_string_constant(module):
    assert _hits((ROOT / module).read_text()) == []
