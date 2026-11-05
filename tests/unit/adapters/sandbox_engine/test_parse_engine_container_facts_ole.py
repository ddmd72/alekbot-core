"""`parse.container_facts` on the real drive child for a legacy OLE2 file (EXTERNAL_FILES D13, owner decision 2026-11-05):
the body carries `status: ole` and `vba_storage`."""
import os
import sys

import pytest

from src.domain.sandbox_contract import SandboxStatus
from tests.support import ole_containers as oc
from tests.unit.adapters.sandbox_engine.test_parse_engine_container_facts import _run

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
                       reason="children run as the invoking user: needs a non-root user"),
]


async def test_an_ole_file_with_macros(tmp_path):
    result, body, _ = await _run(tmp_path, oc.word_with_macros())
    assert (result.status, result.detail) == (SandboxStatus.OK, "ole")
    assert body == {"status": "ole", "members": 4, "vba_storage": True}


async def test_a_plain_ole_file(tmp_path):
    result, body, _ = await _run(tmp_path, oc.word_plain())
    assert (result.status, result.detail) == (SandboxStatus.OK, "ole")
    assert body["vba_storage"] is False
