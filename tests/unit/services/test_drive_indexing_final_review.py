"""Final-review fixes R14 (final_review.md N1, N2): the gone re-listing target is logged; no provider name in the
indexing domain docstrings."""
import logging
from dataclasses import replace

from src.domain import drive_index_planner as planner
from src.domain.user_drive import DriveResyncRequired

from .test_drive_indexing_service import World


class TestRelistingTargetGoneIsLogged:
    """N1: the `DriveItemNotFoundError` handler of the re-listing path must log before it handles."""

    async def test_gone_relisted_folder_logs_one_info_line(self, caplog):
        w = World()
        folder = w.drive.add("dir", folder=True)
        w.drive.add("k.pdf", parent=folder)
        await w.settle()
        await w.repo.upsert_records([planner.schedule(replace(w.rec(folder), reobserve_pending=True),
                                                      w.clock.now, first_seen=True)])
        del w.drive.items[folder]   # gone at the drive; the delta feed has not reported it yet
        with caplog.at_level(logging.INFO):
            await w.tick()
        lines = [r for r in caplog.records if "re-listing target gone" in r.getMessage()]
        assert len(lines) == 1, caplog.text
        assert lines[0].levelno == logging.INFO
        assert "u" in lines[0].getMessage()
        assert not w.rec(folder).reobserve_pending


class TestNoProviderNameInDomainDocstrings:
    """N2: the provider is named only in src/adapters/microsoft/ (plan Global Constraints)."""

    def test_planner_module_docstring(self):
        assert "Graph" not in (planner.__doc__ or "")
        assert "the drive provider" in (planner.__doc__ or "")

    def test_resync_required_docstring(self):
        doc = DriveResyncRequired.__doc__ or ""
        assert "Graph" not in doc
        assert "(cursor expired, HTTP 410)" in doc
