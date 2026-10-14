"""What found each card is shown to Smart (USER_DRIVE_INDEXING_RFC §10.2, final review F5-8).

`matched` was computed but never rendered, and every content hit's window was labelled "matching passage" even when
only the chunk kNN found it (the passage is the nearest one, not one holding the user's words). Smart's folder rule
applies only to a folder its name matched; a folder card near in meaning is not a lead to search inside."""
from src.domain.drive_search import render_results

from .test_drive_search_content_streams import FAR, admit, by_id, index, query_vector, settle_metadata, world  # noqa: F401


async def test_a_nearest_only_content_hit_is_labelled_nearest_passage(monkeypatch):
    w = world()
    scan = w.drive.add("Scan_0012.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "notice period for termination")
    await index(w, scan, ["7.2 Either party may terminate on 30 days notice."], [q])
    text = render_results(await w.search("notice period for termination"))
    assert "nearest passage:\n7.2 Either party" in text and "matching passage" not in text
    matched = next(line for line in text.splitlines() if line.startswith("matched: "))
    assert matched.endswith("content") and "name and" not in matched and matched != "matched: name"


async def test_an_exact_term_hit_is_a_matching_passage(monkeypatch):
    w = world()
    invoice = w.drive.add("Scan_0099.pdf")
    await settle_metadata(w, monkeypatch)
    await index(w, invoice, ["Factura INV-2024/0012 total 1 250,00 €"], [FAR])
    text = render_results(await w.search("INV-2024/0012"))
    assert "matching passage:\nFactura INV-2024/0012" in text and "nearest passage" not in text


async def test_a_name_match_says_so(monkeypatch):
    w = world()
    w.drive.add("Contracts", folder=True)
    await settle_metadata(w, monkeypatch)
    result = await w.search("contracts")
    assert by_id(result) and "matched: name" in render_results(result)


async def test_filter_only_results_carry_no_matched_line(monkeypatch):
    w = world()
    w.drive.add("Contracts", folder=True)
    await settle_metadata(w, monkeypatch)
    result = await w.search("", kind="folder")
    assert result.cards and "matched:" not in render_results(result)
