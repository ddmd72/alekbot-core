"""An Office lock file never answers a search for the document it locks (USER_DRIVE_INDEXING_RFC §5.1, §10.1)."""
from .test_drive_search_service import World, paths


async def test_the_lock_file_is_not_returned_for_its_documents_name():
    w = World()
    contracts = w.drive.add("Contracts", folder=True)
    doc = w.drive.add("Договор_ред3.docx", parent=contracts)
    lock = w.drive.add("~$Договор_ред3.docx", parent=contracts)
    await w.settle()
    assert w.repo.records[("u", lock)] is not None and ("u", lock) not in w.repo.vectors
    found = paths(await w.search("ред3"))
    assert "Contracts/Договор_ред3.docx" in found
    assert not [p for p in found if "~$" in p]
    assert ("u", doc) in w.repo.vectors
