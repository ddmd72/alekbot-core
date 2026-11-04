"""The per-call caps of a passages find (DRIVE_MCP_CONNECTOR_RFC §14.2, E22). Pure.

One value travels from the caller to the selection (`drive_excerpts.select_excerpts`) and to the merge across drives
(`drive_search_merge.merge_excerpts`); absent, it is DEFAULT, the chat's caps, so the chat passes nothing and its
selection is unchanged. WIDE is the connector's `wide` find: about twice the passages and characters per call, the
same per-file cap (the rest of one file comes by batch).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .drive_index_params import (
    EXCERPTS_CHARS_MAX, EXCERPTS_CHARS_MAX_WIDE, EXCERPTS_K, EXCERPTS_K_WIDE, EXCERPTS_PER_FILE_MAX,
)


@dataclass(frozen=True)
class ExcerptCaps:
    """`k`: new chunks per call; `chars`: their characters per call; `per_file`: chunks of one file per call."""
    DEFAULT: ClassVar["ExcerptCaps"]
    WIDE: ClassVar["ExcerptCaps"]

    k: int
    chars: int
    per_file: int


ExcerptCaps.DEFAULT = ExcerptCaps(k=EXCERPTS_K, chars=EXCERPTS_CHARS_MAX, per_file=EXCERPTS_PER_FILE_MAX)
ExcerptCaps.WIDE = ExcerptCaps(k=EXCERPTS_K_WIDE, chars=EXCERPTS_CHARS_MAX_WIDE, per_file=EXCERPTS_PER_FILE_MAX)
