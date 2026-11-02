"""Edges of the cross-drive search (multi-drive 2b): a former drive name in a folder prefix beside a named drive leads
with the rename notice, and a part with no connection answers disconnected."""
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_search import STATUS_DISCONNECTED, STATUS_OK, render_results
from src.services.drive_search_service import _ExcerptStats, _Placed

from .drive_mounts_harness import renamed_acme
from .drive_two_drives_harness import TwoDrives
from .test_drive_search_excerpts_mounts import QUESTION, Passages


async def test_a_former_name_in_the_folder_prefix_beside_a_named_drive_adds_the_notice():
    w = await TwoDrives(acme_history=renamed_acme()).open()
    acme_rd = w.on_acme(w.folder("R&D"))
    w.on_acme(w.file("Tom's plan.docx", kind="docx", parent=acme_rd, chunks=["Acme signs off weekly."]))
    result = await w.find("plan", drives="Acme", folder="Acme Hamburg/R&D")
    assert result.status == STATUS_OK and result.ref_tag == "b2" and len(result.cards) == 1
    assert result.notice == "Acme Hamburg is now called Acme."
    assert render_results(result).splitlines()[0] == "Acme Hamburg is now called Acme."


async def test_a_passages_part_with_no_connection_answers_disconnected():
    w = await Passages().open()
    request = ExcerptRequest(question=QUESTION)
    result = await w.service._excerpts_part(_Placed(mount=None, folder=None), request, frozenset(), _ExcerptStats())
    assert result.status == STATUS_DISCONNECTED and result.request == request and result.files == ()
