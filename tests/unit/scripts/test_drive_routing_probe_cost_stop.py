"""The routing probe has a hard cost stop (cross-cut review R3): `--max-usd` is required, like every paid eval."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.validation import drive_routing_probe as probe  # noqa: E402


def test_max_usd_is_required():
    with pytest.raises(SystemExit):
        probe.build_parser().parse_args(["--runs", "1"])


def test_max_usd_and_runs_are_parsed():
    args = probe.build_parser().parse_args(["--runs", "2", "--max-usd", "3"])
    assert (args.runs, args.max_usd) == (2, 3.0)
