"""The re-listing target the provider does not find is logged and marked (USER_DRIVE_INDEXING_RFC §26.2); no
provider name in the indexing domain docstrings."""
import logging
from dataclasses import replace

from src.domain import drive_index_planner as planner
from src.domain.drive_index import inaccessible
from src.domain.user_drive import DriveResyncRequired

from .test_drive_indexing_service import World


class TestRelistingTargetGoneIsLogged:
    """The `DriveItemNotFoundError` handler of the re-listing path must log before it handles. Since §26.2 (D33) a
    re-listing's "not found" means the bot cannot list the folder: it is marked, never treated as gone."""

    async def test_unlistable_relisted_folder_logs_one_info_line_and_is_marked(self, caplog):
        w = World()
        folder = w.drive.add("dir", folder=True)
        w.drive.add("k.pdf", parent=folder)
        await w.settle()
        await w.repo.upsert_records([planner.schedule(replace(w.rec(folder), reobserve_pending=True),
                                                      w.clock.now, first_seen=True)])
        del w.drive.items[folder]   # the listing by id answers "not found"; the delta feed still decides existence
        listings = []
        real_listing = w.drive.list_children_changes

        async def counted(user_id, folder_id, limit, cursor=None):
            listings.append(folder_id)
            return await real_listing(user_id, folder_id, limit, cursor)
        w.drive.list_children_changes = counted
        with caplog.at_level(logging.INFO):
            await w.tick()
        lines = [r for r in caplog.records if "cannot be listed; marked" in r.getMessage()]
        assert len(lines) == 1, caplog.text
        assert lines[0].levelno == logging.INFO
        assert "u" in lines[0].getMessage() and folder in lines[0].getMessage()
        rec = w.rec(folder)
        assert rec is not None and inaccessible(rec)   # marked, never deleted (§26.2, Δ48)
        # the re-listing waits for access and restarts from its first page (§26.2): not run again while marked
        assert rec.reobserve_pending and rec.reobserve_cursor is None
        assert listings.count(folder) == 1
        await w.tick()
        assert listings.count(folder) == 1


class TestNoProviderNameInDomainDocstrings:
    """The provider is named only in src/adapters/microsoft/ (Step 1 §4.3)."""

    def test_planner_module_docstring(self):
        assert "Graph" not in (planner.__doc__ or "")
        assert "the drive provider" in (planner.__doc__ or "")

    def test_resync_required_docstring(self):
        doc = DriveResyncRequired.__doc__ or ""
        assert "Graph" not in doc
        assert "(cursor expired, HTTP 410)" in doc
