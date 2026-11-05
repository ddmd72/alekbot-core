"""Wire contract of `parse.container_facts` for OLE2 files (owner decision 2026-11-05): the real sandbox app, engine and
child behind `SandboxContentExtractor`, against the in-process reader on the same bytes."""
import os
import sys

import pytest

from src.domain.container_facts import ContainerStatus, container_facts_from_result
from src.utils.document_hardening import container_facts
from tests.support import ole_containers as oc
from tests.unit.adapters.sandbox_client.test_sandbox_container_facts_contract import _over_the_real_app

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"), reason="needs a non-root user"),
]


async def test_the_sandboxed_ole_facts_equal_the_reader_s_in_process(tmp_path):
    sandboxed = _over_the_real_app(tmp_path)
    cases = {"doc": oc.word_with_macros(), "xls": oc.excel_with_macros(), "plain": oc.word_plain(),
             "encrypted": oc.encrypted_ooxml()}
    seen = {}
    for label, data in cases.items():
        local_path = tmp_path / f"{label}.bin"
        local_path.write_bytes(data)
        remote = await sandboxed.container_facts(data)
        assert remote == container_facts_from_result(container_facts(str(local_path))), label
        seen[label] = remote
    assert seen["doc"].carries_macros and seen["xls"].carries_macros
    assert all(seen[k].status is ContainerStatus.OLE and seen[k].checked and not seen[k].carries_macros
               for k in ("plain", "encrypted"))
