"""The drive connector's paging lines (DRIVE_MCP_CONNECTOR_RFC §14.3, §14.4): the next-batch line quoting the arguments
to resend, its `wide` half, the end line where a batch selected nothing new, no "already shown" line for a client, the
search line in both forms, the earlier batches' context treated as seen; and `excerpt_plan`, the plan a render returns
without its text. Over marker texts, never the tokens' wording. Invented items only."""
import json

import pytest

from src.domain import drive_connector_contract as c
from src.domain import drive_excerpt_render as R
from src.domain.drive_connector_paging import ConnectorPaging
from src.domain.drive_connector_result import compose_connector_result
from src.domain.drive_excerpt_context import ExcerptContext, folder_line
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_excerpt_folder_note import FolderNote
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_search import STATUS_AMBIGUOUS_FOLDER, STATUS_DISABLED
from tests.support.drive_connector_texts import marker, marker_texts
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result

TEXTS = marker_texts()
QUESTION = 'What does Tom\'s plan say happens after the "first review"?'
PLAN = excerpt_file("item-1", "R&D/Tom's plan.docx", [chunk("item-1", 2, "After the first review, Acme signs off.")])
RD = FolderNote("f-rd", "R&D", 1, "noted by the owner: «Acme Hamburg specifications»")
NOTED = ExcerptContext(files=(FileContext("item-1", "R&D/Tom's plan.docx", ("f-rd",)),), folders={"f-rd": RD})
REPEATED_TEXT = R.REPEATED.split("{")[0]


def passages(res, cls=OutcomeClass.OK):
    return DriveOutcome(op=DriveOp.PASSAGES, cls=cls, text="(chat render)", success=True,
                        facts=ReadFacts(excerpts=bool(res.files)), excerpts=res)


def compose(outcome, paging=None):
    return compose_connector_result(outcome, TEXTS, nonce=nonces(), paging=paging).split("\n")


def find_paging(*, wide=False, batch=1, folder="", file_refs=""):
    return ConnectorPaging(wide=wide, batch=batch, arguments=(("question", QUESTION), ("folder", folder),
                                                             ("drives", ""), ("file_refs", file_refs)))


# -- the arguments quoted back ----------------------------------------------------------------------------------------

def test_the_next_batch_arguments_quote_the_question_and_scope_and_keep_wide():
    paging = find_paging(wide=True, batch=2, folder="R&D")
    text = paging.next_batch_arguments()
    assert text == (f"question={json.dumps(QUESTION)}, folder=\"R&D\", wide=\"yes\", batch=\"3\"")
    assert '\\"first review\\"' in text   # a quote inside the question cannot end it


def test_empty_arguments_are_not_quoted_and_a_narrow_call_names_no_wide():
    assert find_paging(file_refs="drive:a1:item-1").next_batch_arguments() == (
        f"question={json.dumps(QUESTION)}, file_refs=\"drive:a1:item-1\", batch=\"2\"")


# -- the find's lines -------------------------------------------------------------------------------------------------

def test_more_offers_the_next_batch_with_the_arguments_and_the_wide_half():
    lines = compose(passages(result(PLAN, question=QUESTION, more=True)), find_paging(folder="R&D"))
    nxt = marker(c.BATCH_NEXT).format(batch=2, arguments=find_paging(folder="R&D").next_batch_arguments())
    assert lines[-3:] == [nxt, marker(c.BATCH_NEXT_WIDE), marker(c.LANGUAGE)]
    assert f"question={json.dumps(QUESTION)}" in nxt


def test_a_wide_call_gets_the_next_batch_line_without_the_wide_half():
    lines = compose(passages(result(PLAN, question=QUESTION, more=True)), find_paging(wide=True, batch=4))
    assert marker(c.BATCH_NEXT).format(batch=5, arguments=find_paging(wide=True, batch=4).next_batch_arguments()) \
        in lines
    assert marker(c.BATCH_NEXT_WIDE) not in lines


def test_no_more_no_next_batch_line():
    lines = compose(passages(result(PLAN, question=QUESTION)), find_paging())
    assert not any(line.startswith("<BATCH_") for line in lines)
    assert lines[-1] == marker(c.LANGUAGE)


def test_the_already_shown_line_never_reaches_the_client():
    res = result(PLAN, question=QUESTION, repeated=(("item-1", 1, 0),), more=True, ref_tag="a1")
    with_paging = "\n".join(compose(passages(res), find_paging(batch=2)))
    assert REPEATED_TEXT not in with_paging and "After the first review, Acme signs off." in with_paging
    # the chat's render of the same result keeps it
    assert REPEATED_TEXT in R.render_excerpts(res, nonce=nonces()).text


def test_a_batch_that_selected_nothing_new_answers_the_end_line():
    res = result(question=QUESTION, repeated=(("item-1", 1, 2),))
    lines = compose(passages(res, OutcomeClass.NOT_FOUND), find_paging(batch=3))
    assert marker(c.BATCH_END) in lines
    assert R.NO_PASSAGES not in lines and REPEATED_TEXT not in "\n".join(lines)
    assert not any(line.startswith("<BATCH_NEXT") for line in lines)


def test_nothing_in_scope_at_all_stays_the_no_passages_line():
    lines = compose(passages(result(question=QUESTION), OutcomeClass.NOT_FOUND), find_paging(batch=2))
    assert R.NO_PASSAGES in lines and marker(c.BATCH_END) not in lines


def test_a_batch_does_not_repeat_a_folder_note_an_earlier_batch_showed():
    res = result(PLAN, question=QUESTION, context=NOTED)
    first = "\n".join(compose(passages(res), find_paging()))
    later = "\n".join(compose(passages(result(PLAN, question=QUESTION, context=NOTED,
                                              seen_folders=frozenset({"f-rd"}))), find_paging(batch=2)))
    assert folder_line(RD) in first and folder_line(RD) not in later


def test_a_gate_state_carries_no_paging_line():
    res = result(question=QUESTION, status=STATUS_DISABLED, more=True)
    lines = compose(DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.INDEXING_OFF, text="(off)", success=True,
                                 excerpts=res), find_paging())
    assert not any(line.startswith("<BATCH_") for line in lines)


# -- the search's line ------------------------------------------------------------------------------------------------

def search(more):
    return DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="CARDS", success=True,
                        facts=ReadFacts(cards=2), more=more)


@pytest.mark.parametrize("wide, key", [(False, c.SEARCH_MORE), (True, c.SEARCH_MORE_WIDE)])
def test_a_search_that_showed_fewer_than_it_matched_says_how_to_see_more(wide, key):
    lines = compose(search(True), ConnectorPaging(wide=wide))
    assert lines[-1] == marker(key)
    assert marker(c.SEARCH_MORE if wide else c.SEARCH_MORE_WIDE) not in lines


def test_a_search_that_showed_everything_or_had_no_paging_has_no_line():
    assert not any("SEARCH_MORE" in line for line in compose(search(False), ConnectorPaging()))
    assert not any("SEARCH_MORE" in line for line in compose(search(True)))


# -- the plan without the text ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("res, folders, described", [
    (result(PLAN, question=QUESTION, context=NOTED), frozenset(), frozenset()),
    (result(PLAN, question=QUESTION, context=NOTED), frozenset({"f-rd"}), frozenset({"item-1"})),
    (result(question=QUESTION, status=STATUS_DISABLED), frozenset(), frozenset()),
    (result(question=QUESTION, status=STATUS_AMBIGUOUS_FOLDER,
            request=ExcerptRequest(question=QUESTION, folder="Insurance"), folder_candidates=("Insurance",)),
     frozenset(), frozenset()),
    (result(question=QUESTION, folder_not_indexed=True, folder_path="Code"), frozenset(), frozenset()),
])
def test_excerpt_plan_is_the_plan_the_render_returns(res, folders, described):
    rendered = R.render_excerpts(res, nonce=nonces(), shown_folders=folders, described_files=described)
    assert R.excerpt_plan(res, shown_folders=folders, described_files=described) == rendered.plan
