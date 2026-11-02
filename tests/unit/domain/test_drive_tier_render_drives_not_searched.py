"""The coverage part naming the drives a search did not read (multi-drive 2a, design §3, E-14), and the sentence of a
non-tiered result and of a passages result."""
from dataclasses import replace

from src.domain.drive_collected_result import CollectedResult
from src.domain.drive_excerpt_render import searched_line
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_excerpt_result import ExcerptResult
from src.domain.drive_match import MatchTier
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome, OutcomeClass
from src.domain.drive_search import (
    STATUS_DRIVE_REFUSED, STATUS_OK, DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult,
    render_results,
)
from src.domain.drive_tier_render import coverage_parts
from src.domain.drive_tier_result import CoverageFacts, TierBlock, TieredResult


def _card() -> DriveSearchCard:
    return DriveSearchCard(ref="drive:a1:I001", item_id="I001", name="Tom's plan.docx",
                           path="Drive/R&D/Tom's plan.docx", kind="docx", is_folder=False,
                           summary="Tom's plan.docx | Word document", state="indexed", nonce="nonce0001",
                           tier=MatchTier.ABOUT.value)


def _tiered(coverage: CoverageFacts) -> DriveSearchResult:
    tiered = TieredResult(concepts=("plan",), coverage=coverage, blocks=(TierBlock(MatchTier.ABOUT, 1),))
    return DriveSearchResult(status=STATUS_OK, cards=(_card(),), envelope=DriveSearchEnvelope(),
                             request=DriveSearchRequest(query="plan"), tiered=tiered)


def test_the_part_is_last_and_makes_coverage_incomplete():
    facts = CoverageFacts(unread=2, cut=("later",), not_searched_drives=("Drive 2", "Acme"))
    parts = coverage_parts(facts, passages_tiers_empty=False)
    assert parts[-1] == "drives not searched: Drive 2, Acme"
    assert parts[-2].startswith("words past the first ones")
    text = render_results(_tiered(facts))
    assert "Coverage: incomplete" in text and text.count("drives not searched: Drive 2, Acme.") == 1


def test_the_line_reads_as_the_design_shows_it():
    text = render_results(_tiered(CoverageFacts(not_searched_drives=("Drive 2",))))
    assert "Coverage: incomplete — drives not searched: Drive 2." in text
    assert "Coverage: complete." not in text


def test_no_drive_not_searched_leaves_the_coverage_untouched():
    assert coverage_parts(CoverageFacts(), passages_tiers_empty=False) == []
    assert "Coverage: complete." in render_results(_tiered(CoverageFacts()))
    assert "not searched" not in render_results(_tiered(CoverageFacts()))


def test_a_non_tiered_result_gets_the_envelope_sentence():
    result = DriveSearchResult(status=STATUS_OK, cards=(_card(),), request=DriveSearchRequest(kind="docx"),
                               matched_total=1, envelope=DriveSearchEnvelope(content_search=True,
                                                                           not_searched_drives=("Drive 2", "Acme")))
    assert "Not searched: the drives Drive 2, Acme; name one in drives to search it." in render_results(result)
    quiet = replace(result, envelope=DriveSearchEnvelope(content_search=True))
    assert "Not searched" not in render_results(quiet)


def test_a_refusal_renders_its_line_alone_and_a_notice_leads_the_rest():
    refused = DriveSearchResult(status=STATUS_DRIVE_REFUSED, refusal='There is no drive named "Acme Lviv". '
                                                                    'The drives are: Drive, Acme.')
    assert render_results(refused) == refused.refusal
    noticed = replace(_tiered(CoverageFacts()), notice="Acme Hamburg is now called Acme.")
    assert render_results(noticed).splitlines()[0] == "Acme Hamburg is now called Acme."


def test_the_passages_searched_line_names_the_drives_not_searched():
    request = ExcerptRequest(question="how does the review work")
    excerpts = ExcerptResult(status=STATUS_OK, request=request, not_searched_drives=("Acme",))
    outcome = DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="", success=True, excerpts=excerpts,
                           request_text=request.question)
    line = searched_line([CollectedResult(handle="r1", step=1, kind="passages", outcome=outcome)])
    assert line.endswith("Not searched: the drives Acme; name one in drives to search it.")
    quiet = replace(outcome, excerpts=replace(excerpts, not_searched_drives=()))
    assert "Not searched" not in searched_line([CollectedResult(handle="r1", step=1, kind="passages", outcome=quiet)])
