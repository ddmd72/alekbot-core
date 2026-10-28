"""A11: the run's ledger keeps every retrieved passage once and builds the one block the orchestrator reads."""
import re

from src.domain.drive_excerpt_context_plan import ContextPlan
from src.domain.drive_excerpt_ledger import ExcerptLedger
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from tests.unit.domain.drive_excerpt_builders import LEASE, TOMS_PLAN, chunk, excerpt_file, nonces, result


def _plan_file(*indexes, item_id="item-1", path=TOMS_PLAN):
    return excerpt_file(item_id, path, [chunk(item_id, i, f"Passage {i} of {path}.") for i in indexes])


def test_an_empty_ledger_is_false():
    ledger = ExcerptLedger()
    assert not ledger and ledger.keys() == frozenset() and ledger.files() == ()


def test_chunks_dedup_across_adds():
    ledger = ExcerptLedger()
    ledger.add(result(_plan_file(0, 1)), ContextPlan.EMPTY)
    ledger.add(result(_plan_file(1, 2)), ContextPlan.EMPTY)
    assert ledger
    assert ledger.keys() == {("item-1", 1, 0), ("item-1", 1, 1), ("item-1", 1, 2)}
    assert [c.index for c in ledger.files()[0].chunks] == [0, 1, 2]


def test_files_in_first_retrieval_order_chunks_in_index_order():
    ledger = ExcerptLedger()
    ledger.add(result(_plan_file(5), _plan_file(0, item_id="item-2", path=LEASE)), ContextPlan.EMPTY)
    ledger.add(result(_plan_file(3, item_id="item-2", path=LEASE), _plan_file(1)), ContextPlan.EMPTY)
    files = ledger.files()
    assert [f.item_id for f in files] == ["item-1", "item-2"]
    assert [c.index for c in files[0].chunks] == [1, 5]
    assert [c.index for c in files[1].chunks] == [0, 3]


def test_the_block_holds_every_chunk_added_with_runs_merged():
    ledger = ExcerptLedger()
    ledger.add(result(_plan_file(2)), ContextPlan.EMPTY)
    ledger.add(result(_plan_file(3), _plan_file(0, item_id="item-2", path=LEASE)), ContextPlan.EMPTY)
    ledger.add(result(_plan_file(9)), ContextPlan.EMPTY)
    block = ledger.block(nonce=nonces())
    for i in (2, 3, 9):
        assert f"Passage {i} of {TOMS_PLAN}." in block
    assert f"Passage 0 of {LEASE}." in block
    heads = [line for line in block.splitlines() if line.startswith("[id ")]
    assert [h.split(" · ")[0] for h in heads] == ["[id drive:item-1:3-4", "[id drive:item-1:10-10",
                                                  "[id drive:item-2:1-1"]
    assert len(re.findall(r"<drive_document nonce=\"nonce", block)) == 2


def test_folders_shown_and_files_described_accumulate_from_the_plans():
    ledger = ExcerptLedger()
    ledger.add(result(_plan_file(0)), ContextPlan(shown_after=frozenset({"folder-1"}),
                                                  described=frozenset({"item-1"})))
    ledger.add(result(_plan_file(1)), ContextPlan(shown_after=frozenset({"folder-1", "folder-2"}),
                                                  described=frozenset({"item-2"})))
    assert ledger.folders_shown() == {"folder-1", "folder-2"}
    assert ledger.files_described() == {"item-1", "item-2"}


class TestCollector:
    def _outcome(self, res):
        return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="…", success=True, excerpts=res,
                            excerpt_plan=ContextPlan(shown_after=frozenset({"folder-1"})))

    def test_a_passages_outcome_with_files_fills_the_ledger(self):
        collector = PassThroughCollector()
        collector.add(1, "passages", self._outcome(result(_plan_file(0, 1))))
        assert collector.ledger.keys() == {("item-1", 1, 0), ("item-1", 1, 1)}
        assert collector.ledger.folders_shown() == {"folder-1"}

    def test_an_outcome_without_files_leaves_it_empty(self):
        collector = PassThroughCollector()
        collector.add(1, "passages", self._outcome(result()))
        collector.add(2, "search", DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="…", success=True))
        assert not collector.ledger and collector.ledger.folders_shown() == frozenset()

    def test_a_missing_plan_counts_as_empty(self):
        collector = PassThroughCollector()
        collector.add(1, "passages", DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="…", success=True,
                                                  excerpts=result(_plan_file(0))))
        assert collector.ledger and collector.ledger.folders_shown() == frozenset()
