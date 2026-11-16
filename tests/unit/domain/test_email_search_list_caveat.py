"""A list result carries the card caveat (G3 review F2, slice 8): what a card line can be trusted for is the same in a list
as in a ranked result, so the list says it after its count line, as tool text, and the ranked head is unchanged.
Invented cast only."""
from datetime import date

from src.domain.email_search_fold import fold
from src.domain.email_search_merge import GROUP_ABOUT, Candidate
from src.domain.email_search_render import SECTION_MAIL, SECTION_TOOL, render_list
from src.domain.email_search_texts import CARD_CAVEAT, HEAD_RANKED
from src.domain.email_search_time import TimeWindow
from tests.unit.domain.email_search_fixtures import invoice, receipts

SEPTEMBER = TimeWindow("period", date(2026, 9, 1), date(2026, 9, 30), True, "September 2026")


def _cand(card):
    return Candidate(card=card, group=GROUP_ABOUT, lists=1, z=None, hit_keys=())


def test_the_caveat_is_the_ranked_heads_own_sentences():
    assert HEAD_RANKED == "Indexed mail, best match first. " + CARD_CAVEAT
    assert CARD_CAVEAT.startswith("Each card was written from its mail when it was indexed")
    assert CARD_CAVEAT.endswith("Open the mail before quoting a detail that is not on its card.")


def test_a_list_says_the_caveat_after_its_count_line_as_tool_text():
    cards = [receipts()[-1], invoice(), receipts()[-2]]
    out = render_list(SEPTEMBER, fold([_cand(c) for c in cards]), count=3, uncarded=0)
    lines = out.text.splitlines()
    assert lines[0] == "3 indexed mails for September 2026, all shown, newest first."
    assert lines[1] == CARD_CAVEAT and lines[2].startswith("• [msg_receipt_2026-09-22]")
    assert out.sections[0].kind == SECTION_TOOL and out.sections[0].text.endswith(CARD_CAVEAT)
    assert out.sections[1].kind == SECTION_MAIL and CARD_CAVEAT not in out.sections[1].text


def test_an_empty_period_has_no_caveat():
    out = render_list(SEPTEMBER, [], count=0, uncarded=0)
    assert out.text == "No indexed mail for September 2026."
