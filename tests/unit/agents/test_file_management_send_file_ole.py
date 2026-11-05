"""`send_file_to_user` and OLE2 macros (owner decision 2026-11-05): an email attachment with a VBA storage is refused
under any name and nothing is uploaded; one without goes through."""
import pytest

from src.domain import active_kind as ak
from src.domain.agent import AgentStatus
from src.domain.container_facts import ContainerFacts, ContainerStatus
from src.domain.email import EmailPart
from src.domain.email_open_texts import active_kind_refusal_note
from src.domain.email_ref import format_email_ref
from tests.support import ole_containers as oc
from tests.unit.agents.test_file_management_send_file import MSG, _send
from tests.unit.agents.test_file_management_send_file import world as base_world  # noqa: F401

OLE_FILES = {
    "21": ("invoice_bathroom_es.doc", "application/msword", oc.word_with_macros()),
    "22": ("invoice_bathroom_es.txt", "text/plain", oc.excel_with_macros()),
    "23": ("invoice_bathroom_es.doc", "application/msword", oc.word_plain()),
}


@pytest.fixture
async def world(base_world):  # noqa: F811
    for pid, (name, mime, data) in OLE_FILES.items():
        base_world.mailbox.messages[MSG].append(
            EmailPart(part_id=pid, filename=name, mime=mime, size=len(data), handle=f"h{pid}"))
        base_world.mailbox.blobs[f"h{pid}"] = data
    return base_world


@pytest.mark.parametrize("pid", ["21", "22"])
async def test_an_ole_attachment_with_macros_is_refused(world, pid):
    world.containers.facts = ContainerFacts(status=ContainerStatus.OLE, members=4, vba_storage=True)
    response = await _send(world, format_email_ref(MSG, pid))
    assert response.status == AgentStatus.SUCCESS
    assert response.result == active_kind_refusal_note(ak.OFFICE_MACROS)
    assert world.notify.calls == []


async def test_an_ole_attachment_without_macros_is_sent(world):
    world.containers.facts = ContainerFacts(status=ContainerStatus.OLE, members=2)
    await _send(world, format_email_ref(MSG, "23"))
    (call,) = world.notify.calls
    assert call["filename"] == "invoice_bathroom_es.doc"
