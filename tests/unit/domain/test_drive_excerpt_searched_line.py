"""A12: on a passages run, code writes what was searched from the collector, never from the model."""
from src.domain.drive_answer_texts import SEARCHED_PREFIX
from src.domain.drive_collected_result import CollectedResult
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_render import searched_line
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.user_drive import DriveItem
from tests.unit.domain.drive_excerpt_builders import LEASE, TOMS_PLAN, chunk, excerpt_file, result


def _r(step, op, **fields):
    outcome = DriveOutcome(op=op, cls=OutcomeClass.OK, text="…", success=True, **fields, ref_tag="a1")
    return CollectedResult(handle=f"r{step}", step=step, kind=op.value, outcome=outcome)


def _find(step, question, **request):
    req = ExcerptRequest(question=question, **request)
    folder_path = request.get("folder", "")
    return _r(step, DriveOp.PASSAGES, excerpts=result(excerpt_file(chunks=[chunk(text="T.")]), request=req,
                                                      folder_path=folder_path), request_text=question)


def test_the_whole_ladder_in_order():
    rng = ExcerptRangeRequest(file_ref="drive:item-1", first=4, last=6)
    results = [
        _r(1, DriveOp.SEARCH, request_text="review process"),
        _find(2, "How does the review process work?", folder="R&D"),
        _find(3, "What happens after the first review?", folder="R&D"),
        _r(4, DriveOp.PASSAGES, excerpts=result(excerpt_file(chunks=[chunk(index=3, text="T.")]), request=rng)),
        _r(5, DriveOp.OPEN, items=(DriveItem(item_id="item-2", name=LEASE, path=LEASE, is_folder=False),)),
    ]
    assert searched_line(results) == (
        f'{SEARCHED_PREFIX}folder "R&D"; passage questions: "How does the review process work?" | '
        '"What happens after the first review?"; neighbours: "R&D/Tom\'s plan.docx" passages 4–6; '
        'opened: "lease_porto.pdf"; searches: "review process"')


def test_the_whole_drive_when_no_scope_was_given():
    assert searched_line([_find(1, "Who signs Tom's plan?")]) == \
        f'{SEARCHED_PREFIX}the whole drive; passage questions: "Who signs Tom\'s plan?"'


def test_scoped_files_are_named_by_path():
    line = searched_line([_find(1, "Who signs?", file_refs=("drive:item-1",))])
    assert line.startswith(f'{SEARCHED_PREFIX}files "{TOMS_PLAN}"; ')


def test_a_quote_in_a_question_is_escaped():
    line = searched_line([_find(1, 'What does "final" mean in «R&D»?')])
    assert 'passage questions: "What does \\"final\\" mean in \\«R&D\\»?"' in line


def test_a_repeated_question_is_named_once():
    line = searched_line([_find(1, "Who signs?"), _find(2, "Who signs?")])
    assert line.count('"Who signs?"') == 1


def test_no_reads_no_line():
    assert searched_line([]) == ""
    assert searched_line([_r(1, DriveOp.LIST)]) == ""
