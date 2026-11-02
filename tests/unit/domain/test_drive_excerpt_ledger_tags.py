"""The passages ledger keys files and chunks by (ref tag, item, generation, index): the same item id on two
connections is two files (MULTI_DRIVE_RFC section 9, design E-4)."""
from dataclasses import replace

from src.domain.drive_excerpt_context_plan import ContextPlan
from src.domain.drive_excerpt_ledger import ExcerptLedger
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result


def _file(tag, *indexes, item_id="item-1"):
    file = excerpt_file(item_id, f"Passage of {tag}", [chunk(item_id, i, f"Text {tag} {i}.") for i in indexes],
                        ref_tag=tag)
    return replace(file, ref_tag=tag)


def test_item_one_on_two_connections_never_suppresses_the_other():
    ledger = ExcerptLedger()
    ledger.add(result(_file("a1", 0, 1)), ContextPlan.EMPTY)
    assert ledger.keys() == {("a1", ("item-1", 1, 0)), ("a1", ("item-1", 1, 1))}
    ledger.add(result(_file("b2", 0)), ContextPlan.EMPTY)
    assert ("b2", ("item-1", 1, 0)) in ledger.keys() and len(ledger.keys()) == 3
    files = ledger.files()
    assert [(f.ref_tag, [c.index for c in f.chunks]) for f in files] == [("a1", [0, 1]), ("b2", [0])]


def test_both_files_reach_the_block():
    ledger = ExcerptLedger()
    ledger.add(result(_file("a1", 0)), ContextPlan.EMPTY)
    ledger.add(result(_file("b2", 0)), ContextPlan.EMPTY)
    block = ledger.block(nonce=nonces())
    assert "Text a1 0." in block and "Text b2 0." in block


def test_a_repeated_chunk_is_matched_under_the_tag_of_the_file_that_showed_it():
    ledger = ExcerptLedger()
    ledger.add(result(_file("b2", 0)), ContextPlan.EMPTY)
    ledger.add(result(repeated=(("item-1", 1, 0),)), ContextPlan.EMPTY)
    assert ledger._matched == {("b2", ("item-1", 1, 0))}
    ledger.add(result(repeated=(("item-1", 1, 5),), ref_tag="a1"), ContextPlan.EMPTY)
    assert ("a1", ("item-1", 1, 5)) in ledger._matched


def test_a_repeated_chunk_uses_its_per_chunk_tag():
    ledger = ExcerptLedger()
    ledger.add(result(repeated=(("item-1", 1, 0), ("item-1", 1, 0)), repeated_tags=("a1", "b2")), ContextPlan.EMPTY)
    assert ledger._matched == {("a1", ("item-1", 1, 0)), ("b2", ("item-1", 1, 0))}
