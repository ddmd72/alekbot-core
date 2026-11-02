"""Merging the searches of several connections (MULTI_DRIVE_RFC section 11.2, 11.4, E9)."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_excerpt_chunk import ExcerptChunk
from src.domain.drive_excerpt_file import ExcerptFile
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_excerpt_result import ExcerptResult
from src.domain.drive_index_params import EXCERPTS_K, RELATED_MAX
from src.domain.drive_match import MatchTier
from src.domain.drive_mount import Mount
from src.domain.drive_search import (
    STATUS_DISABLED, STATUS_OK, DriveSearchCard, DriveSearchEnvelope, DriveSearchResult, render_results,
)
from src.domain.drive_search_merge import merge_excerpts, merge_results
from src.domain.drive_tier_render import render_tiered
from src.domain.drive_tier_result import CoverageFacts, TierBlock, TieredResult
from tests.unit.services.drive_mounts_harness import drive_connection

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PLAN = "Drive/R&D/Tom's plan.docx"
PLAN_ON_ACME = "Acme/R&D/Tom's plan.docx"
DRIVE = Mount(drive_connection("conn-1", "a1", "Drive", drive_identity="x1", created_at=NOW), primary=True)
ACME = Mount(drive_connection("conn-2", "b2", "Acme", drive_identity="x2", created_at=NOW), primary=False)


def card(tag, item, path, tier=MatchTier.ABOUT, digest=None):
    return DriveSearchCard(ref=f"drive:{tag}:{item}", item_id=item, name=path.rsplit("/", 1)[-1], path=path, kind="docx",
                           is_folder=False, summary="s", tier=tier.value, content_hash=digest, ref_tag=tag,
                           nonce=f"nonce{item}x0000")


def part(cards, *, coverage=None, absent=(), in_scope=(), totals=None, **env):
    blocks = []
    for tier in dict.fromkeys(MatchTier(c.tier) for c in cards):
        n = sum(1 for c in cards if c.tier == tier.value)
        blocks.append(TierBlock(tier=tier, total=(totals or {}).get(tier, n)))
    tiered = TieredResult(concepts=("plan",), coverage=coverage or CoverageFacts(), blocks=tuple(blocks),
                          absent=tuple(absent), absent_in_scope=tuple(in_scope))
    return DriveSearchResult(status=STATUS_OK, cards=tuple(cards), tiered=tiered, envelope=DriveSearchEnvelope(**env))


def merged(parts, not_searched=()):
    return merge_results(parts, not_searched=not_searched)


def test_cards_by_tier_then_primary_first_then_own_rank():
    acme = part([card("b2", "a1", "Acme/x", MatchTier.ABOUT), card("b2", "a2", "Acme/y", MatchTier.MENTIONED)])
    drive = part([card("a1", "d1", "Drive/p", MatchTier.MENTIONED), card("a1", "d2", "Drive/q", MatchTier.ABOUT)])
    result = merged([(ACME, acme), (DRIVE, drive)])
    assert [c.item_id for c in result.cards] == ["d2", "a1", "d1", "a2"]
    assert [c.ref_tag for c in result.cards] == ["a1", "b2", "a1", "b2"] and result.ref_tag == ""


def test_tier_totals_are_summed_and_rest_concatenated():
    one = part([card("a1", "d1", "Drive/p")], totals={MatchTier.ABOUT: 5})
    two = part([card("b2", "a1", "Acme/x")], totals={MatchTier.ABOUT: 3})
    block = merged([(DRIVE, one), (ACME, two)]).tiered.block(MatchTier.ABOUT)
    assert block.total == 8


def test_coverage_sums_ints_ors_bools_and_unions_tuples():
    a = part([], coverage=CoverageFacts(unread=1, full_page=True, cut=("x",)))
    b = part([], coverage=CoverageFacts(unread=2, building=True, cut=("x", "y")))
    cov = merged([(DRIVE, a), (ACME, b)]).tiered.coverage
    assert (cov.unread, cov.full_page, cov.building, cov.cut) == (3, True, True, ("x", "y"))


def test_absent_is_absent_on_every_part_and_the_rest_is_in_scope():
    a = part([], absent=("lease", "tax"))
    b = part([], absent=("lease",), in_scope=("tax",))
    tiered = merged([(DRIVE, a), (ACME, b)]).tiered
    assert tiered.absent == ("lease",) and tiered.absent_in_scope == ("tax",)


def test_a_part_that_could_not_be_searched_is_named_and_the_others_answer():
    off = DriveSearchResult(status=STATUS_DISABLED)
    ok = part([card("a1", "d1", "Drive/p")])
    result = merged([(DRIVE, ok), (ACME, off)], not_searched=("Drive 2",))
    assert result.status == STATUS_OK and len(result.cards) == 1
    assert result.envelope.not_searched_drives == ("Drive 2", "Acme")
    assert result.tiered.coverage.not_searched_drives == ("Drive 2", "Acme")
    assert "Drive 2, Acme" in render_tiered(result)


def test_all_parts_not_ok_returns_the_first_parts_status():
    first = DriveSearchResult(status=STATUS_DISABLED)
    result = merged([(ACME, DriveSearchResult(status="disconnected")), (DRIVE, first)])
    assert result is first


def test_equal_content_collapses_to_the_primarys_card_with_also_on():
    on_drive = part([card("a1", "d1", PLAN, MatchTier.MENTIONED, "fakehash:h:10")])
    on_acme = part([card("b2", "a9", PLAN_ON_ACME, MatchTier.ABOUT, "fakehash:h:10")], totals={MatchTier.ABOUT: 1})
    result = merged([(ACME, on_acme), (DRIVE, on_drive)])
    assert len(result.cards) == 1
    only = result.cards[0]
    assert only.ref == "drive:a1:d1" and only.also_on == (PLAN_ON_ACME,)
    assert only.tier == MatchTier.ABOUT.value                      # the best tier of the copies
    assert result.tiered.block(MatchTier.ABOUT).total == 1 and result.tiered.block(MatchTier.MENTIONED) is None
    assert f"also on: {PLAN_ON_ACME}" in render_tiered(result)


def test_without_a_primary_the_first_mount_by_name_keeps_the_card():
    drive = Mount(DRIVE.connection, primary=False)
    a = part([card("a1", "d1", PLAN, digest="fakehash:h:10")])
    b = part([card("b2", "a9", PLAN_ON_ACME, digest="fakehash:h:10")])
    result = merged([(drive, a), (ACME, b)])
    assert result.cards[0].ref_tag == "b2" and result.cards[0].also_on == (PLAN,)


def test_different_hash_families_are_shown_twice():
    a = part([card("a1", "d1", PLAN, digest="fakehash:h:10")])
    b = part([card("b2", "a9", PLAN_ON_ACME, digest="sha256:h:10")])
    result = merged([(DRIVE, a), (ACME, b)])
    assert len(result.cards) == 2 and all(not c.also_on for c in result.cards)


def test_empty_files_are_not_copies_of_each_other():
    a = part([card("a1", "d1", "Drive/e", digest="fakehash:h:0")])
    b = part([card("b2", "a9", "Acme/e", digest="fakehash:h:0")])
    assert len(merged([(DRIVE, a), (ACME, b)]).cards) == 2


def test_related_cards_are_capped_after_the_merge():
    many = lambda tag, n: part([card(tag, f"{tag}{i}", f"p{i}", MatchTier.RELATED) for i in range(n)])
    result = merged([(DRIVE, many("a1", RELATED_MAX)), (ACME, many("b2", RELATED_MAX))])
    assert len(result.cards) == RELATED_MAX and render_results(result)


def test_a_filter_only_listing_sums_matches_less_the_collapsed_copies():
    a = replace(DriveSearchResult(status=STATUS_OK, cards=(card("a1", "d1", PLAN, MatchTier.ABOUT, "f:h:1"),)),
                matched_total=4)
    b = replace(DriveSearchResult(status=STATUS_OK, cards=(card("b2", "a9", PLAN_ON_ACME, MatchTier.ABOUT, "f:h:1"),)),
                matched_total=3, matched_at_least=True)
    result = merged([(DRIVE, a), (ACME, b)])
    assert result.matched_total == 6 and result.matched_at_least and result.tiered is None


def test_a_single_part_keeps_its_result_and_names_the_unselected_drives():
    one = part([card("a1", "d1", PLAN)])
    result = merged([(DRIVE, one)], not_searched=("Acme",))
    assert result.cards == one.cards and result.envelope.not_searched_drives == ("Acme",)
    assert result.tiered.coverage.not_searched_drives == ("Acme",)


# ------------------------------------------------------------------------------------------ passages

def chunk(item, index, text="x"):
    return ExcerptChunk(item, 1, index, text, 0, len(text))


def efile(tag, item, path, indexes=(0,), digest=None, text="x"):
    return ExcerptFile(item_id=item, ref=f"drive:{tag}:{item}", name=path, path=path, kind="docx", generation=1,
                       chunk_count=5, content_hash=digest, ref_tag=tag, chunks=tuple(chunk(item, i, text) for i in indexes))


REQUEST = ExcerptRequest(question="how")


def eresult(*files, **kw):
    return ExcerptResult(status=STATUS_OK, request=REQUEST, files=tuple(files), **kw)


def emerge(parts, not_searched=()):
    return merge_excerpts(parts, request=REQUEST, not_searched=not_searched, no_text_listed=10)


def test_files_are_ordered_by_best_distance_across_connections():
    drive = (DRIVE, eresult(efile("a1", "d1", "Drive/a"), efile("a1", "d2", "Drive/b")), {"d1": 0.4, "d2": 0.9})
    acme = (ACME, eresult(efile("b2", "x1", "Acme/c")), {"x1": 0.1})
    result = emerge([drive, acme])
    assert [f.item_id for f in result.files] == ["x1", "d1", "d2"] and result.ref_tag == ""


def test_identical_files_on_two_connections_are_one_file_with_the_other_path():
    drive = (DRIVE, eresult(efile("a1", "d1", PLAN, (0,), "f:h:9")), {"d1": 0.4})
    acme = (ACME, eresult(efile("b2", "x1", PLAN_ON_ACME, (0, 1), "f:h:9")), {"x1": 0.1})
    (kept,) = emerge([acme, drive]).files
    assert kept.ref_tag == "a1" and kept.also_at == (PLAN_ON_ACME,)
    assert [c.index for c in kept.chunks] == [0, 1] and {c.item_id for c in kept.chunks} == {"d1"}


def test_the_per_call_cap_applies_after_the_merge():
    many = lambda tag, n: eresult(*(efile(tag, f"{tag}{i}", f"p{i}") for i in range(n)))
    drive = (DRIVE, many("a1", EXCERPTS_K), {f"a1{i}": 0.5 for i in range(EXCERPTS_K)})
    acme = (ACME, many("b2", EXCERPTS_K), {f"b2{i}": 0.1 for i in range(EXCERPTS_K)})
    files = emerge([drive, acme]).files
    assert len(files) == EXCERPTS_K and {f.ref_tag for f in files} == {"b2"}


def test_a_part_that_could_not_be_read_is_named_not_searched():
    drive = (DRIVE, eresult(efile("a1", "d1", "Drive/a")), {"d1": 0.4})
    off = (ACME, ExcerptResult(status=STATUS_DISABLED, request=REQUEST), {})
    assert emerge([drive, off], ("Drive 2",)).not_searched_drives == ("Drive 2", "Acme")


def test_all_parts_unreadable_returns_the_first_result():
    first = ExcerptResult(status=STATUS_DISABLED, request=REQUEST)
    assert emerge([(ACME, ExcerptResult(status="disconnected", request=REQUEST), {}), (DRIVE, first, {})]) is first


def test_repeated_ids_keep_the_tag_of_their_connection():
    on_drive = eresult(efile("a1", "d1", "Drive/a"), repeated=(("item-1", 1, 0),), repeated_tags=("a1",))
    on_acme = eresult(efile("b2", "x1", "Acme/c"), repeated=(("item-1", 1, 0),), repeated_tags=("b2",))
    result = emerge([(DRIVE, on_drive, {"d1": 0.1}), (ACME, on_acme, {"x1": 0.2})])
    assert result.repeated == (("item-1", 1, 0), ("item-1", 1, 0)) and result.repeated_tags == ("a1", "b2")
    assert result.ref_tag == ""
