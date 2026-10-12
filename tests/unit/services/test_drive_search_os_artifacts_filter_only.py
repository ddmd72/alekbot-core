"""OS and lock artefacts are kept as records but never shown, filter-only searches included: a date, kind or folder
filter reads records, not cards, so it must still hide them (USER_DRIVE_INDEXING_RFC §5.1, §10.1)."""
from datetime import datetime, timezone

import pytest

from .test_drive_search_service import World, paths

ARTEFACTS = ("~$Договор_ред3.docx", ".~lock.plan.odt#", "Thumbs.db", "desktop.ini", ".DS_Store", "draft.tmp")


async def _world():
    w = World()
    work = w.drive.add("Work", folder=True)
    w.drive.add("plan.odt", parent=work)
    for name in ARTEFACTS:
        w.drive.add(name, parent=work)
    await w.settle()
    return w


@pytest.mark.parametrize("filters", [
    dict(date_from=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    dict(kind="database"),
    dict(kind="document"),
    dict(folder="Work"),
    dict(folder="Work", date_from=datetime(2026, 1, 1, tzinfo=timezone.utc)),
])
async def test_filter_only_never_returns_an_os_artefact(filters):
    w = await _world()
    found = paths(await w.search("", **filters))
    assert not [p for p in found if p.rsplit("/", 1)[-1] in ARTEFACTS], found


async def test_the_records_are_kept():
    w = await _world()
    names = {r.name for r in w.repo.records.values()}
    assert set(ARTEFACTS) <= names
