"""compose_connector_result and cap_result (DRIVE_MCP_CONNECTOR_RFC §5.5, §6): which lines a result holds and in which
order, for each outcome kind, over marker texts (never the tokens' wording). Invented items only."""
import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_connector_contract as c
from src.domain import drive_excerpt_render as R
from src.domain.drive_access_policy import OUTSIDE_SCOPE_LINE
from src.domain.drive_connector_result import RESULT_MAX_CHARS, cap_result, compose_connector_result
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_withheld import Withheld
from src.domain.user_drive import DriveItem
from tests.support.drive_connector_texts import marker, marker_texts
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result

TEXTS = marker_texts()
PLAN = DriveItem(item_id="d1", name="Tom's plan.docx", path="R&D/Tom's plan.docx", is_folder=False, size_bytes=1000)
VIDEO = DriveItem(item_id="v1", name="standup.mp4", path="R&D/standup.mp4", is_folder=False, size_bytes=4000,
                  mime_type="video/mp4")


def head(status: str, line: str) -> list:
    return [marker(c.STATUS).format(status=status), marker(c.WHAT_TO_DO).format(line=line)]


NEUTRAL = [marker(c.STATUS).format(status="refused"), marker(c.WHAT_TO_DO).format(line=marker(c.REFUSED_GUIDANCE)),
           marker(c.REFUSED)]


def reading(*keys: str) -> list:
    return [t.READING_PREFIX + marker(k) for k in keys]


def compose(outcome: DriveOutcome) -> list:
    return compose_connector_result(outcome, TEXTS, nonce=nonces()).split("\n")


class TestSearch:
    def test_cards_answer_from_the_results_with_the_card_lines_in_order(self):
        outcome = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="CARDS FA-0137", success=True,
                               facts=ReadFacts(cards=2, passage=True))
        assert compose(outcome) == [
            *head("ok", marker(c.ANSWER_FROM_RESULTS)),
            *reading("R_FIND", "R_TIERS", "R_TRUST", "R_RELATED", "R_COVERAGE", "R_MANY", "R_ALL", "R_PASSAGE",
                     "R_VERSIONS"),
            "CARDS FA-0137"]

    def test_a_not_found_search_still_shows_its_text_and_the_not_searched_lines(self):
        outcome = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.NOT_FOUND, text="No file holds FA-0137.",
                               success=True, facts=ReadFacts(coverage_incomplete=True))
        assert compose(outcome) == [*head("not_found", marker(c.ANSWER_FROM_RESULTS)),
                                    *reading("R_BUILDING", "R_NOT_SEARCHED", "R_FOLDER_CARD"),
                                    "No file holds FA-0137."]

    def test_a_request_error_is_the_line_to_correct_and_no_block(self):
        outcome = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.REQUEST_ERROR, text="date_from: use YYYY-MM-DD.",
                               success=False)
        assert compose(outcome) == head("refused", "date_from: use YYYY-MM-DD.")


class TestPassages:
    def test_found_passages_are_re_rendered_then_the_language_line(self):
        file = excerpt_file("item-1", "R&D/Tom's plan.docx", [chunk("item-1", 0, "After the first review, Tom sends invoice FA-0137.")])
        res = result(file)
        outcome = DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="(chat render)", success=True,
                               facts=ReadFacts(excerpts=True), excerpts=res)
        rendered = R.render_excerpts(res, nonce=nonces(), page_next=marker(c.PAGE_NEXT)).text
        assert compose(outcome) == [*head("ok", marker(c.ANSWER_FROM_RESULTS)), *reading("R_EXCERPTS"),
                                    *rendered.split("\n"), marker(c.LANGUAGE)]

    def test_a_page_prints_the_connector_next_page_line_not_the_chat_one(self):
        span = PageSpan(first=1, last=10, total=30, version=2, next=11, path="R&D/Tom's plan.docx", ref="drive:item-1")
        file = excerpt_file("item-1", "R&D/Tom's plan.docx", [chunk("item-1", i, f"Step {i + 1}.") for i in (0, 1)])
        res = result(file, request=ExcerptRangeRequest("drive:item-1", 1, 10, page=True), page=span)
        outcome = DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="(chat render)", success=True,
                               facts=ReadFacts(excerpts=True, paged=True), excerpts=res)
        lines = compose(outcome)
        assert lines[2:3] == reading("R_PAGED")
        assert R.page_line(span, page_next=marker(c.PAGE_NEXT)) in lines
        assert R.page_line(span) not in lines
        assert lines[-1] == marker(c.LANGUAGE)

    def test_no_passage_found_keeps_the_state_text_and_no_language_line(self):
        res = result()
        outcome = DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.NOT_FOUND, text="(chat render)", success=True,
                               excerpts=res)
        lines = compose(outcome)
        assert lines[:2] == head("not_found", marker(c.ANSWER_FROM_RESULTS))
        assert lines[2:] == R.render_excerpts(res, nonce=nonces(), page_next=marker(c.PAGE_NEXT)).text.split("\n")
        assert marker(c.LANGUAGE) not in lines


class TestOpen:
    def test_a_body_answers_from_the_result_with_the_open_lines(self):
        outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="Step 1. Send invoice FA-0137.",
                               success=True, items=(PLAN,), ref_tag="a1", body=True)
        assert compose(outcome) == [*head("ok", marker(c.ANSWER_FROM_RESULTS)),
                                    *reading("R_NUMBERS", "R_DOC_VS_MEMORY", "R_NOT_READ"),
                                    "Step 1. Send invoice FA-0137."]

    def test_too_long_routes_with_the_file_ref_and_keeps_the_refusal_text(self):
        outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text="R&D/Tom's plan.docx is too long.",
                               success=False, items=(PLAN,), ref_tag="a1", facts=ReadFacts(too_long=True))
        assert compose(outcome) == [*head("refused", marker(c.ROUTE).format(ref="drive:a1:d1")),
                                    *reading("R_TOO_LONG"), "R&D/Tom's plan.docx is too long."]

    @pytest.mark.parametrize("code", list(Withheld))
    def test_a_withheld_file_answers_its_code_line_and_its_label(self, code):
        outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="", success=True, items=(VIDEO,),
                               ref_tag="a1", withheld=code)
        assert compose(outcome) == [*head("ok", marker(c.withheld_key(code))), outcome.label_of(VIDEO)]

    def test_a_ref_that_is_not_a_drive_ref_answers_the_neutral_refusal(self):
        outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text="", success=False)
        assert compose(outcome) == NEUTRAL


class TestRefusalsAndFailures:
    def test_a_chat_refusal_line_never_reaches_the_client(self):
        line = OUTSIDE_SCOPE_LINE.format(drive="Acme")
        outcome = DriveOutcome(op=DriveOp.LIST, cls=OutcomeClass.REFUSED, text=line, success=False)
        lines = compose(outcome)
        assert lines == [*head("refused", marker(c.REFUSED_GUIDANCE)), marker(c.REFUSED)]
        assert line not in "\n".join(lines)

    @pytest.mark.parametrize("op, text", [(DriveOp.OPEN, "The file is gone."), (DriveOp.LIST, "No folder R&D/x."),
                                          (DriveOp.NOTE_READ, "There is no note on drive:a1:x9."),
                                          (DriveOp.PASSAGES, "That ref resolves to no drive.")])
    def test_an_item_that_does_not_exist_answers_the_same_neutral_refusal(self, op, text):
        outcome = DriveOutcome(op=op, cls=OutcomeClass.NOT_FOUND, text=text, success=False)
        lines = compose(outcome)
        assert lines == NEUTRAL and text not in "\n".join(lines)

    def test_the_index_note_that_the_bot_cannot_open_a_file_keeps_its_text(self):
        outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text="The bot cannot open this file.",
                               success=False, facts=ReadFacts(inaccessible=True))
        assert compose(outcome) == [*head("refused", "The bot cannot open this file."), *reading("R_NO_ACCESS")]

    def test_a_call_that_did_not_finish_says_so_and_nothing_else(self):
        outcome = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.NOT_FINISHED, text="", success=False)
        assert compose(outcome) == head("unfinished", marker(c.UNFINISHED))

    def test_a_failure_is_its_own_line(self):
        outcome = DriveOutcome(op=DriveOp.LIST, cls=OutcomeClass.NOT_CONNECTED, text="Connect the drive.",
                               success=False)
        assert compose(outcome) == head("not_connected", "Connect the drive.")

    def test_a_missing_text_raises_instead_of_dropping_a_line(self):
        texts = marker_texts()
        partial = type(texts)(tools=texts.tools, results={k: v for k, v in texts.results.items() if k != "R_FIND"})
        outcome = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="CARDS", success=True,
                               facts=ReadFacts(cards=1))
        with pytest.raises(KeyError):
            compose_connector_result(outcome, partial)


class TestCap:
    OVER = "<OVER_SIZE>"

    def test_a_result_within_the_limit_is_unchanged(self):
        text = "a\n" * 10
        assert cap_result(text, self.OVER, limit=len(text)) == text

    def test_a_long_result_is_cut_at_a_line_and_ends_with_the_over_size_line(self):
        lines = [f"line {i:04d} of invoice FA-0137" for i in range(100)]
        text = "\n".join(lines)
        out = cap_result(text, self.OVER, limit=500)
        assert len(out) <= 500 and out.endswith("\n" + self.OVER)
        kept = out.split("\n")[:-1]
        assert kept == lines[:len(kept)] and len(kept) > 0

    def test_a_head_with_no_line_break_is_cut_mid_line(self):
        out = cap_result("x" * 1000, self.OVER, limit=100)
        assert len(out) == 100 and out.endswith("\n" + self.OVER)

    def test_the_default_limit_is_the_connector_bound(self):
        text = "y\n" * (RESULT_MAX_CHARS // 2 + 10)
        out = cap_result(text, self.OVER)
        assert len(out) <= RESULT_MAX_CHARS == 100_000 and out.endswith(self.OVER)
