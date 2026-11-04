"""The per-call caps of a search's result (DRIVE_MCP_CONNECTOR_RFC §14.2, §14.6, E22). Pure.

One value travels from the caller to the service's shown counts per tier (`DriveSearchService`) and to the render's
character cut (`drive_tier_render.render_tiered`); absent, it is DEFAULT, the chat's caps, so the chat passes nothing
and its result is unchanged. WIDE is the connector's `wide` search: about twice the files shown per tier and a larger
result. Related is not a shown tier here: it stays RELATED_MAX either way, and the candidate fetch caps never change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .drive_index_params import (
    ABOUT_SHOWN, ABOUT_SHOWN_WIDE, LABELLED_SHOWN, LABELLED_SHOWN_WIDE, MENTIONED_SHOWN, MENTIONED_SHOWN_WIDE,
    RESULT_CHARS_MAX, RESULT_CHARS_MAX_WIDE, TRANSLATED_SHOWN, TRANSLATED_SHOWN_WIDE,
)
from .drive_match import MatchTier


@dataclass(frozen=True)
class SearchCaps:
    """Files shown per tier (About, Mentioned in, By translation, Labelled) and the result's characters."""
    DEFAULT: ClassVar["SearchCaps"]
    WIDE: ClassVar["SearchCaps"]

    about: int
    mentioned: int
    translated: int
    labelled: int
    result_chars: int

    def shown(self, tier: MatchTier) -> int:
        """The shown count of one of the four shown tiers; KeyError for another (Related has its own cap)."""
        return {MatchTier.ABOUT: self.about, MatchTier.MENTIONED: self.mentioned,
                MatchTier.TRANSLATED: self.translated, MatchTier.LABELLED: self.labelled}[tier]


SearchCaps.DEFAULT = SearchCaps(about=ABOUT_SHOWN, mentioned=MENTIONED_SHOWN, translated=TRANSLATED_SHOWN,
                                labelled=LABELLED_SHOWN, result_chars=RESULT_CHARS_MAX)
SearchCaps.WIDE = SearchCaps(about=ABOUT_SHOWN_WIDE, mentioned=MENTIONED_SHOWN_WIDE,
                             translated=TRANSLATED_SHOWN_WIDE, labelled=LABELLED_SHOWN_WIDE,
                             result_chars=RESULT_CHARS_MAX_WIDE)
