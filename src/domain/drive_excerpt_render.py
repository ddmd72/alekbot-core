"""
Passages as text (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A10, A12, A18). Pure: the caller supplies every
nonce.

Outside a `<drive_document>` wrapper go only code-written lines; a file's text goes inside its own wrapper, one per
file. Paths in code lines are quoted (`drive_excerpt_context.quoted`). The "where it comes from" lines (A18) follow
the context plan the render returns: noted folders before their first file, then the file's head lines and its own
note outside the wrapper; the file's description first inside it.

Public interface:
    render_excerpts(result, *, nonce, shown_folders, described_files) -> RenderedExcerpts   one call's tool result
    render_block(files, context, *, nonce) -> str       the consolidated block the ledger builds for the orchestrator
    searched_line(results) -> str                       A12: what a passages run did, from the collector
    head_line(file, run) -> str                         one run's `[id <ref>:<i>-<j> · <path> · passages i–j of N]`
"""
from __future__ import annotations

from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from . import drive_answer_texts as answer_texts
from .drive_capture import IMAGE_TEXT_CAPTURES, born_digital
from .drive_collected_result import CollectedResult
from .drive_excerpt_chunk import display_number
from .drive_excerpt_context import ExcerptContext, folder_line, plan_context, quoted
from .drive_excerpt_context_plan import ContextPlan
from .drive_excerpt_file import ExcerptFile
from .drive_excerpt_file_context import FileContext
from .drive_excerpt_range_request import ExcerptRangeRequest
from .drive_excerpt_rendered import RenderedExcerpts
from .drive_excerpt_request import ExcerptRequest
from .drive_excerpt_result import ExcerptResult
from .drive_excerpt_run import ExcerptRun
from .drive_excerpts import display_text, merge_runs
from .drive_index_params import CARD_ACCURACY_TRUST, EXCERPT_FOLDER_NOTES_CHARS_MAX
from .drive_op import DriveOp
from .drive_search import (
    REFERENCE_REMINDER, STATUS_AMBIGUOUS_FOLDER, STATUS_OK, DriveSearchRequest, DriveSearchResult, escape_text,
    render_results, wrap_untrusted,
)
from .drive_search_texts import REBUILDING_LINE
from .drive_tool_texts import READ_PASSAGES
from .user_drive import make_drive_ref

OPENING_FIND = "Passages nearest to the question: excerpts of the indexed text, not whole files."
OPENING_RANGE = "Passages read by number: excerpts of the indexed text, not whole files."
OPENING_BLOCK = "Passages retrieved for this request: excerpts of the files' indexed text, not whole files."
SCOPE_FOLDER = "Searched only within the requested folder and its subfolders."
SCOPE_FILES = "Searched only within the requested files."
BUILDING = "The index is still being built: some files may not be in it yet."
NO_TEXT = "{count} files in scope have no indexed text, so their content was not searched: {paths}"
NO_PASSAGES = "No indexed passage matched in this scope."
REPEATED = "Already shown earlier in this request and matched again: {ids}"
CONTINUE = "Cut at the size limit: " + READ_PASSAGES + " from passage {number} continues."
AMBIGUOUS = "Several folders match {wanted}. Ask the user which one, or ask again within one of them: {paths}"
NOT_INDEXED = ("The files inside this folder are not indexed (a code repository, generated files, a folder too "
               "large to index, or one being removed), so no passage can come from it. Browse it.")
HEAD = "[id {ref}:{first}-{last} · {path} · passages {first}–{last} of {total}{marks}]"
RUN_MARK = "[passages {first}–{last}]"
MARK_IMAGE = " · read from an image"
MARK_UNKNOWN = " · capture unknown"
MARK_ACCURACY = " · card accuracy {percent}%"
STALE = "stale: the file changed after it was indexed"
TRUNCATED = "truncated: only the beginning of the file was indexed"
ALSO_AT = "identical copies also at: {paths}"
DESCRIPTION = "what it is (read from the file): {text}"
DROPPED = "Folder notes not shown (limit): {paths}"
SEARCHED_WHOLE_DRIVE = "the whole drive"
SEARCHED_FOLDER = "folder {path}"
SEARCHED_FILES = "files {paths}"
SEARCHED_PARTS = ("{scope}", "passage questions: {questions}", "neighbours: {neighbours}", "opened: {opened}",
                  "searches: {searches}")


def _low_accuracy(file: ExcerptFile) -> bool:
    return file.card_accuracy is not None and file.card_accuracy < CARD_ACCURACY_TRUST


def _marks(file: ExcerptFile) -> str:
    marks = ""
    if file.capture in IMAGE_TEXT_CAPTURES:
        marks += MARK_IMAGE
    elif not born_digital(file.capture):
        marks += MARK_UNKNOWN
    if _low_accuracy(file):
        marks += MARK_ACCURACY.format(percent=file.card_accuracy)
    return marks


def head_line(file: ExcerptFile, run: ExcerptRun) -> str:
    return HEAD.format(ref=file.ref, first=run.first, last=run.last, path=quoted(file.path or "/"),
                       total=file.chunk_count, marks=_marks(file))


def _file_block(file: ExcerptFile, nonce: str, plan: ContextPlan, context: Optional[FileContext]) -> List[str]:
    """Folder lines, head lines and the file's own note outside the wrapper (code and the owner's words); the
    description, the text and the file's state lines inside it."""
    described = context is not None and file.item_id in plan.described
    runs = merge_runs(file.chunks)
    body: List[str] = []
    # a card scored below the trust bar was graded on this very reading: its description may carry a wrong value
    if described and context is not None and context.description and not _low_accuracy(file):
        body.append(DESCRIPTION.format(text=context.description))
    for run in runs:
        body.append(RUN_MARK.format(first=run.first, last=run.last))
        body.append(display_text(run.text, file.kind))
    if file.stale:
        body.append(STALE)
    if file.truncated:
        body.append(TRUNCATED)
    if file.also_at:
        body.append(ALSO_AT.format(paths="; ".join(file.also_at)))
    own_note = [escape_text(context.own_note)] if described and context is not None and context.own_note else []
    return [*(folder_line(note) for note in plan.before.get(file.item_id, ())),
            *(head_line(file, run) for run in runs), *own_note,
            wrap_untrusted("\n".join(body), nonce=nonce, ref=file.ref, path=file.path)]


def _planned(files: Sequence[ExcerptFile], context: ExcerptContext, *, shown_folders: FrozenSet[str],
             described_files: FrozenSet[str]) -> Tuple[List[str], ContextPlan, Dict[str, FileContext]]:
    """The context plan over the files that print, and the dropped-folders line when the cap dropped any."""
    by_id = {f.item_id: f for f in context.files}
    printed = [by_id[f.item_id] for f in files if f.chunks and f.item_id in by_id]
    plan = plan_context(printed, context.folders, shown_folders=shown_folders, described_files=described_files,
                        chars_max=EXCERPT_FOLDER_NOTES_CHARS_MAX)
    dropped = [DROPPED.format(paths=", ".join(quoted(p) for p in plan.dropped))] if plan.dropped else []
    return dropped, plan, by_id


def _files(files: Sequence[ExcerptFile], nonce: Callable[[], str], plan: ContextPlan,
           by_id: Mapping[str, FileContext]) -> List[str]:
    out: List[str] = []
    for file in files:
        if file.chunks:
            out.extend(_file_block(file, nonce(), plan, by_id.get(file.item_id)))
    return out


def _gate_text(result: ExcerptResult) -> Optional[str]:
    """The search's own text for a state that searched nothing (statuses reuse the search's)."""
    if result.status == STATUS_OK and (result.awaiting_rebuild or result.wrong_space):
        return REBUILDING_LINE
    if result.status in (STATUS_OK, STATUS_AMBIGUOUS_FOLDER):
        return None
    folder = result.request.folder if isinstance(result.request, ExcerptRequest) else ""
    return render_results(DriveSearchResult(status=result.status, request=DriveSearchRequest(folder=folder or None),
                                            other_drive=result.other_drive,
                                            folder_lookup_truncated=result.folder_lookup_truncated))


def _ids(result: ExcerptResult) -> str:
    return ", ".join(f"{make_drive_ref(item_id)}:{display_number(index)}" for item_id, _, index in result.repeated)


def render_excerpts(result: ExcerptResult, *, nonce: Callable[[], str], shown_folders: FrozenSet[str] = frozenset(),
                    described_files: FrozenSet[str] = frozenset()) -> RenderedExcerpts:
    gate = _gate_text(result)
    if gate is not None:
        return RenderedExcerpts(text=gate, plan=ContextPlan.EMPTY)
    if result.status == STATUS_AMBIGUOUS_FOLDER:
        wanted = quoted(result.request.folder if isinstance(result.request, ExcerptRequest) else "")
        text = AMBIGUOUS.format(wanted=wanted, paths=", ".join(quoted(p) for p in result.folder_candidates))
        return RenderedExcerpts(text=text, plan=ContextPlan.EMPTY)
    ranged = isinstance(result.request, ExcerptRangeRequest)
    out: List[str] = [REFERENCE_REMINDER, OPENING_RANGE if ranged else OPENING_FIND]
    if result.building:
        out.append(BUILDING)
    if result.folder_path:
        out.append(SCOPE_FOLDER)
    elif isinstance(result.request, ExcerptRequest) and result.request.file_refs:
        out.append(SCOPE_FILES)
    if result.folder_not_indexed:
        out.append(NOT_INDEXED)
        return RenderedExcerpts(text="\n".join(out), plan=ContextPlan.EMPTY)
    if result.no_text:
        out.append(NO_TEXT.format(count=result.no_text, paths=", ".join(quoted(p) for p in result.no_text_paths)))
    if result.repeated:
        out.append(REPEATED.format(ids=_ids(result)))
    dropped, plan, by_id = _planned(result.files, result.context, shown_folders=shown_folders,
                                    described_files=described_files)
    blocks = _files(result.files, nonce, plan, by_id)
    if not blocks and not result.repeated:
        out.append(NO_PASSAGES)
    out.extend(dropped)
    out.extend(blocks)
    if result.continue_at is not None:
        out.append(CONTINUE.format(number=result.continue_at))
    return RenderedExcerpts(text="\n".join(out), plan=plan)


def render_block(files: Sequence[ExcerptFile], context: ExcerptContext, *, nonce: Callable[[], str]) -> str:
    """Every file once, in the order given, each in its own wrapper (A11); the context re-planned over the whole
    run, so each noted folder prints once, right before the first file under it."""
    dropped, plan, by_id = _planned(files, context, shown_folders=frozenset(), described_files=frozenset())
    return "\n".join([REFERENCE_REMINDER, OPENING_BLOCK, *dropped, *_files(files, nonce, plan, by_id)])


# -- A12 the Searched line ------------------------------------------------------------------------------------------

def _scope(result: ExcerptResult) -> str:
    request = result.request
    if not isinstance(request, ExcerptRequest):
        return ""
    if request.folder:
        return SEARCHED_FOLDER.format(path=quoted(result.folder_path or request.folder))
    if request.file_refs:
        paths = [f.path for f in result.files] or list(request.file_refs)
        return SEARCHED_FILES.format(paths=", ".join(quoted(p) for p in paths))
    return SEARCHED_WHOLE_DRIVE


def _neighbours(result: ExcerptResult, request: ExcerptRangeRequest, items_path: str) -> str:
    path = result.files[0].path if result.files else items_path or request.file_ref
    return f"{quoted(path)} passages {request.first}–{request.last}"


def searched_line(results: Sequence[CollectedResult]) -> str:
    scopes: List[str] = []
    questions: List[str] = []
    neighbours: List[str] = []
    opened: List[str] = []
    searches: List[str] = []

    def add(bucket: List[str], value: str) -> None:
        if value and value not in bucket:
            bucket.append(value)

    for r in results:
        outcome = r.outcome
        if outcome.op is DriveOp.PASSAGES and outcome.excerpts is not None:
            excerpts = outcome.excerpts
            if isinstance(excerpts.request, ExcerptRangeRequest):
                add(neighbours, _neighbours(excerpts, excerpts.request, outcome.items[0].path if outcome.items else ""))
            else:
                add(scopes, _scope(excerpts))
                add(questions, quoted(outcome.request_text or excerpts.request.question))
        elif outcome.op is DriveOp.OPEN:
            for item in outcome.items:
                add(opened, quoted(item.path or item.name))
        elif outcome.op is DriveOp.SEARCH and outcome.request_text:
            add(searches, quoted(outcome.request_text))
    values = {"scope": ", ".join(scopes), "questions": " | ".join(questions), "neighbours": "; ".join(neighbours),
              "opened": ", ".join(opened), "searches": " | ".join(searches)}
    parts = [part.format(**values) for part, key in zip(SEARCHED_PARTS, values) if values[key]]
    return answer_texts.SEARCHED_PREFIX + "; ".join(parts) if parts else ""
