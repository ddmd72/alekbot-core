"""google-cloud-storage is pinned (AGENT_WORKSPACE_RFC §5.3, §13): the workspace adapter relies on
if_generation_match / versions= / generation= semantics of the installed 3.9.0."""
import pytest
from pathlib import Path

_REQ = Path(__file__).resolve().parents[2] / "requirements.txt"


@pytest.mark.datafile
def test_pinned():
    lines = [line.split("#", 1)[0].strip() for line in _REQ.read_text().splitlines()]
    assert "google-cloud-storage==3.9.0" in lines
    assert "google-cloud-storage" not in lines
