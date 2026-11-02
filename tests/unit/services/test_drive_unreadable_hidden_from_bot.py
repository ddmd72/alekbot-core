"""The bot is told only about documents "not read yet", never about those that could not be read
(decisions/drive_unreadable_files_cabinet_only.md; USER_DRIVE_INDEXING_RFC §10.2, §25.5.6): the envelope and the
coverage facts have no unread count, the lines never mention it, an index with only unreadable files is complete
for the bot, and the indexer's counters still carry the number for the Cabinet."""
import dataclasses
from dataclasses import replace

from src.domain.drive_search import (
    NOT_READ_YET_COUNTER, STATUS_OK, UNREAD_COUNTER, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult,
    render_results,
)
from src.domain.drive_search_merge import _merge_coverage, _merge_envelope
from src.domain.drive_tier_render import coverage_parts
from src.domain.drive_tier_result import CoverageFacts
from src.infrastructure.drive_operations import _search_facts

from .drive_index_fakes import U_SCOPE
from .test_drive_search_service import World


def _render(**env):
    return render_results(DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(query="lease"),
                                            envelope=DriveSearchEnvelope(**env)))


class TestNoUnreadCount:
    def test_neither_value_object_has_the_field(self):
        assert "unread" not in {f.name for f in dataclasses.fields(DriveSearchEnvelope)}
        assert "unread" not in {f.name for f in dataclasses.fields(CoverageFacts)}

    def test_the_envelope_still_says_not_read_yet_and_nothing_about_unreadable(self):
        text = _render(not_read_yet=5)
        assert "5 documents are not read yet, so their content was not searched." in text
        assert "could not be read" not in text

    def test_the_coverage_line_still_says_not_read_yet_and_nothing_about_unreadable(self):
        parts = coverage_parts(CoverageFacts(not_read_yet=2), passages_tiers_empty=False)
        assert parts == ["2 documents are not read yet"]

    def test_the_merge_sums_not_read_yet_without_an_unread_sum(self):
        a, b = DriveSearchEnvelope(not_read_yet=2), DriveSearchEnvelope(not_read_yet=5)
        assert _merge_envelope([a, b], ()).not_read_yet == 7
        assert _merge_coverage([CoverageFacts(not_read_yet=2), CoverageFacts(not_read_yet=5)], a, ()).not_read_yet == 7


class TestCoverageIncomplete:
    def _result(self, **env):
        return DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(query="lease"),
                                 envelope=DriveSearchEnvelope(**env))

    def test_an_index_with_only_unreadable_files_is_complete(self):
        assert _search_facts(self._result()).coverage_incomplete is False

    def test_not_read_yet_still_makes_it_incomplete(self):
        assert _search_facts(self._result(not_read_yet=2)).coverage_incomplete is True


class TestEnvelopeFromCounters:
    async def test_only_the_waiting_part_reaches_the_bot(self):
        w = World()
        w.drive.add("doc.pdf")
        await w.settle()
        state = await w.repo.get_sync_state(U_SCOPE)
        w.repo.states["u"] = replace(state, counters={**state.counters, UNREAD_COUNTER: 5, NOT_READ_YET_COUNTER: 3})
        result = await w.search("doc")
        assert result.envelope.not_read_yet == 3
        assert "could not be read" not in render_results(result)

    async def test_unreadable_files_alone_say_nothing(self):
        w = World()
        w.drive.add("doc.pdf")
        await w.settle()
        state = await w.repo.get_sync_state(U_SCOPE)
        w.repo.states["u"] = replace(state, counters={**state.counters, UNREAD_COUNTER: 2})
        result = await w.search("doc")
        text = render_results(result)
        assert result.envelope.not_read_yet == 0
        assert "could not be read" not in text and "not read yet" not in text
