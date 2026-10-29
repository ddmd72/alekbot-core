"""
Passages as text (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A10, A12, A18). Pure: the caller supplies every
nonce.

Outside a `<drive_document>` wrapper go only code-written lines; a file's text goes inside its own wrapper, one per
file. Paths in code lines are quoted (`drive_excerpt_context.quoted`). The "where it comes from" lines (A18) follow
the context plan the render returns: noted folders before their first file, then the file's head lines and its own
note outside the wrapper; the file's description first inside it.

Public interface:
    render_excerpts(result, *, nonce, shown_folders, described_files) -> RenderedExcerpts   one call's tool result
    render_block(files, context, *, nonce, pages, matched) -> str   the consolidated block the ledger builds for the orchestrator
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
from .drive_excerpt_page import PageSpan, last_page_start
from .drive_index_lean import ChunkId
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
OPENING_BLOCK_PAGES = "Pages read in order: the files' indexed text, a page at a time, not whole files."
OPENING_BLOCK_MIXED = ("Passages retrieved for this request and pages read in order: excerpts of the files' indexed "
                       "text, not whole files; each file's \"pages read\" line says which is which.")
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
PAGE_NEXT = ("Page: passages {first}–{last} of {total} of {path}, indexed text version {version}. "
             "Next page: " + READ_PASSAGES + " passages=\"from {next}\".")
PAGE_END = "Page: passages {first}–{last} of {total} of {path}, indexed text version {version}: the end of the file."
PAGE_END_TRUNCATED = ("Page: passages {first}–{last} of {total} of {path}, indexed text version {version}: the end "
                      "of the indexed text. The file goes on past it; that part cannot be read.")
PAGE_STALE = " The file changed after this version was indexed."
PAGE_PAST_END = ("{path} has {total} passages (indexed text version {version}); passage {first} is past the end. "
                 "The last page starts at passage {last_page}.")
PAGES_READ = "pages read: passages {ranges} of {total}, indexed text version {version}"
PAGES_READ_NEXT = "; next page: from {next}"
PAGES_READ_END = "; the end of the file"
PAGES_READ_END_TRUNCATED = "; the end of the indexed text: the file goes on past it, and that part cannot be read"
NEAREST = " · nearest the question: {numbers}"
PAGES_READ_STALE = "; the file changed after this version was indexed"
SEARCHED_PAGE_NEXT = "{path} passages {first}–{last} (next: from {next})"
SEARCHED_PAGE_END = "{path} passages {first}–{last} (the end)"
SEARCHED_PAGE_PAST_END = "{path} from passage {first} (past the end)"
SEARCHED_PARTS = ("{scope}", "passage questions: {questions}", "neighbours: {neighbours}", "pages: {pages}",
                  "opened: {opened}", "searches: {searches}")


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


def _file_block(file: ExcerptFile, nonce: str, plan: ContextPlan, context: Optional[FileContext],
                pages_line: str = "") -> List[str]:
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
            *([pages_line] if pages_line else []),
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
           by_id: Mapping[str, FileContext], pages_line: Callable[[ExcerptFile], str] = lambda f: "") -> List[str]:
    out: List[str] = []
    for file in files:
        if file.chunks:
            out.extend(_file_block(file, nonce(), plan, by_id.get(file.item_id), pages_line(file)))
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
        out.append(page_past_end(result.page) if result.page is not None and result.page.past_end else NO_PASSAGES)
    out.extend(dropped)
    out.extend(blocks)
    if result.page is not None:
        if not result.page.past_end:
            out.append(page_line(result.page))
    elif result.continue_at is not None:
        out.append(CONTINUE.format(number=result.continue_at))
    return RenderedExcerpts(text="\n".join(out), plan=plan)


def page_past_end(page: PageSpan) -> str:
    return PAGE_PAST_END.format(path=quoted(page.path or "/"), total=page.total, version=page.version,
                                first=page.first, last_page=last_page_start(page.total))


def page_line(page: PageSpan) -> str:
    """The code line after a page's blocks (§29.7): where the page stands and where the next one starts."""
    numbers = dict(first=page.first, last=page.last, total=page.total, path=quoted(page.path or "/"),
                   version=page.version, next=page.next)
    text = (PAGE_END_TRUNCATED if page.end and page.truncated else PAGE_END if page.end else PAGE_NEXT).format(**numbers)
    return text + (PAGE_STALE if page.stale else "")


def _runs(numbers: Sequence[int]) -> str:
    runs: List[List[int]] = []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)


def _pages_read(spans: Sequence[PageSpan], matched_numbers: Sequence[int]) -> str:
    """One file's `pages read` line (§29.7): merged ranges, the latest page's continuation, the matched chunks."""
    delivered = [n for sp in spans if not sp.past_end for n in range(sp.first, sp.last + 1)]
    latest = spans[-1]
    text = PAGES_READ.format(ranges=_runs(delivered), total=latest.total, version=latest.version)
    if latest.end:
        text += PAGES_READ_END_TRUNCATED if latest.truncated else PAGES_READ_END
    else:
        text += PAGES_READ_NEXT.format(next=latest.next)
    if matched_numbers:
        text += NEAREST.format(numbers=_runs(matched_numbers))
    if latest.stale:
        text += PAGES_READ_STALE
    return text


def render_block(files: Sequence[ExcerptFile], context: ExcerptContext, *, nonce: Callable[[], str],
                 pages: Optional[Mapping[Tuple[str, int], Sequence[PageSpan]]] = None,
                 matched: FrozenSet[ChunkId] = frozenset()) -> str:
    """Every file once, in the order given, each in its own wrapper (A11); the context re-planned over the whole
    run, so each noted folder prints once, right before the first file under it. With page spans (§29.6 L11-L12) the
    opening follows the mix and a paged file gets its `pages read` line; without, the output is the find-only block."""
    dropped, plan, by_id = _planned(files, context, shown_folders=frozenset(), described_files=frozenset())
    spans = {key: value for key, value in (pages or {}).items() if value}

    def pages_line(file: ExcerptFile) -> str:
        own = spans.get((file.item_id, file.generation))
        if not own:
            return ""
        numbers = [display_number(i) for (item, gen, i) in matched if item == file.item_id and gen == file.generation]
        return _pages_read(own, numbers)

    opening = OPENING_BLOCK if not spans else OPENING_BLOCK_MIXED if matched else OPENING_BLOCK_PAGES
    return "\n".join([REFERENCE_REMINDER, opening, *dropped, *_files(files, nonce, plan, by_id, pages_line)])


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


def _page_entry(result: ExcerptResult, request: ExcerptRangeRequest, items_path: str) -> str:
    page = result.page
    path = quoted((page.path if page is not None and page.path else "") or items_path or request.file_ref)
    if page is None or page.past_end:
        return SEARCHED_PAGE_PAST_END.format(path=path, first=request.first)
    if page.end:
        return SEARCHED_PAGE_END.format(path=path, first=page.first, last=page.last)
    return SEARCHED_PAGE_NEXT.format(path=path, first=page.first, last=page.last, next=page.next)


def searched_line(results: Sequence[CollectedResult]) -> str:
    scopes: List[str] = []
    questions: List[str] = []
    neighbours: List[str] = []
    pages: List[str] = []
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
                items_path = outcome.items[0].path if outcome.items else ""
                if excerpts.request.page:
                    add(pages, _page_entry(excerpts, excerpts.request, items_path))
                else:
                    add(neighbours, _neighbours(excerpts, excerpts.request, items_path))
            else:
                add(scopes, _scope(excerpts))
                add(questions, quoted(outcome.request_text or excerpts.request.question))
        elif outcome.op is DriveOp.OPEN:
            for item in outcome.items:
                add(opened, quoted(item.path or item.name))
        elif outcome.op is DriveOp.SEARCH and outcome.request_text:
            add(searches, quoted(outcome.request_text))
    values = {"scope": ", ".join(scopes), "questions": " | ".join(questions), "neighbours": "; ".join(neighbours),
              "pages": "; ".join(pages), "opened": ", ".join(opened), "searches": " | ".join(searches)}
    parts = [part.format(**values) for part, key in zip(SEARCHED_PARTS, values) if values[key]]
    return answer_texts.SEARCHED_PREFIX + "; ".join(parts) if parts else ""
