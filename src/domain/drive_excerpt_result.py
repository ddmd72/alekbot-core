"""The typed result of one passages call (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A5-A10). Pure.

Statuses are the search's (`drive_search.STATUS_*`). `other_drive` mirrors `DriveSearchResult.other_drive`;
`wrong_space` is the index's vectors being in another embedding space (plan D-P4), kept apart from D30's
`awaiting_rebuild`; both answer as rebuilding. An ambiguous folder carries its candidate paths and no files."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Union

from .drive_excerpt_context import ExcerptContext
from .drive_excerpt_file import ExcerptFile
from .drive_excerpt_range_request import ExcerptRangeRequest
from .drive_excerpt_request import ExcerptRequest
from .drive_index_lean import ChunkId


@dataclass(frozen=True)
class ExcerptResult:
    """`repeated`: chunks shown earlier in the run that ranked again; `no_text`: files in scope with no indexed text
    (`no_text_paths` names them); `continue_at`: the display number a cut range continues from."""
    status: str
    request: Union[ExcerptRequest, ExcerptRangeRequest]
    files: Tuple[ExcerptFile, ...] = ()
    repeated: Tuple[ChunkId, ...] = ()
    no_text: int = 0
    no_text_paths: Tuple[str, ...] = ()
    building: bool = False
    awaiting_rebuild: bool = False
    other_drive: bool = False
    wrong_space: bool = False
    folder_path: str = ""
    folder_candidates: Tuple[str, ...] = ()
    folder_lookup_truncated: bool = False
    folder_not_indexed: bool = False
    continue_at: Optional[int] = None
    context: ExcerptContext = ExcerptContext.EMPTY
