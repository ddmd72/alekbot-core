"""Small domain edges of slice 2b: the card's `also on` line, summed conjunction counts of a cross-drive merge, an item
outside an outcome's own items taking the outcome's `ref_tag`."""
from datetime import datetime, timezone

from src.domain.drive_match import MatchTier
from src.domain.drive_mount import Mount
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search import STATUS_OK, DriveSearchCard, DriveSearchEnvelope, DriveSearchResult, render_results
from src.domain.drive_search_merge import merge_results
from src.domain.drive_tier_result import TierBlock, TieredResult
from src.domain.user_drive import DriveItem
from tests.unit.services.drive_mounts_harness import drive_connection

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
DRIVE = Mount(drive_connection("conn-1", "a1", "Drive", drive_identity="x1", created_at=NOW), primary=True)
ACME = Mount(drive_connection("conn-2", "b2", "Acme", drive_identity="x2", created_at=NOW), primary=False)


def _card(tag="a1", item="d1", path="Drive/R&D/Tom's plan.docx", **over):
    return DriveSearchCard(ref=f"drive:{tag}:{item}", item_id=item, name="Tom's plan.docx", path=path, kind="docx",
                           is_folder=False, summary="s", tier=MatchTier.ABOUT.value, ref_tag=tag,
                           nonce=f"nonce{item}x0000", **over)


def test_a_card_with_copies_on_another_drive_prints_the_also_on_line():
    card = _card(also_on=("Acme/R&D/Tom's plan.docx",))
    result = DriveSearchResult(status=STATUS_OK, cards=(card,), envelope=DriveSearchEnvelope(),
                               tiered=TieredResult(concepts=("plan",), blocks=(TierBlock(MatchTier.ABOUT, 1),)))
    lines = render_results(result).splitlines()
    assert "also on: Acme/R&D/Tom's plan.docx" in lines


def _part(tag, item, conjunction):
    tiered = TieredResult(concepts=("coimbra", "trip"), blocks=(TierBlock(MatchTier.ABOUT, 1),),
                          conjunction=conjunction)
    return DriveSearchResult(status=STATUS_OK, cards=(_card(tag, item),), tiered=tiered,
                             envelope=DriveSearchEnvelope())


def test_conjunction_counts_are_summed_per_word_across_drives():
    merged = merge_results([(DRIVE, _part("a1", "d1", (("coimbra", 41), ("trip", 2)))),
                            (ACME, _part("b2", "a1", (("coimbra", 4), ("rome", 7))))], not_searched=())
    assert dict(merged.tiered.conjunction) == {"coimbra": 45, "trip": 2, "rome": 7}


def test_an_item_outside_the_outcome_takes_the_outcomes_ref_tag():
    plan = DriveItem(item_id="item-1", name="Tom's plan.docx", path="R&D/Tom's plan.docx", is_folder=False)
    stray = DriveItem(item_id="item-9", name="menu.pdf", path="menu.pdf", is_folder=False)
    out = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="", success=True, items=(plan,),
                       item_tags=("b2",), ref_tag="a1")
    assert out.ref_of(plan) == "drive:b2:item-1"
    assert out.ref_of(stray) == "drive:a1:item-9"
