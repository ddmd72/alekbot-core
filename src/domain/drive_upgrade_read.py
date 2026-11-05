"""
Which index runs are an "upgrade re-read" (docs/10_rfcs/EXTERNAL_FILES_RFC.md §7 U5, owner decision O-3 = (b)). Pure.

A record is read again because its parser stamp (`extracted_by` = "parser@N") went stale, while its content was not
edited since that stamp was decided. Only such a re-read loses the paid reader: an edited file keeps it.

What proves "not edited" (read from `drive_index_planner`):
- a record holding a readable generation: `indexed_version == content_version` (`indexed()` copies the content version
  it was built from; an edit moves `content_version` alone);
- any other record (demoted, failed, no generation): `dirty_since is None`. An edit of a record that waits for content
  sets `dirty_since` (`_quiet_period`) and only a settled content outcome clears it (`_settled`); `reextraction_due`
  never sets it. So a settled record with `dirty_since` unset holds the outcome of its current content version.
Known gap: the bot's own atomic write (a nudge) never sets `dirty_since`; such a record reads as an upgrade.

Public interface:
    upgrade_reread(rec, versions) -> bool
"""
from __future__ import annotations

from typing import Mapping

from .document_extraction import PARSER
from .drive_index import DriveIndexRecord
from .drive_index_planner import extractor_stale


def upgrade_reread(rec: DriveIndexRecord, versions: Mapping[str, int]) -> bool:
    """True when `rec` is stamped by a stale `parser@N` link and its content was not edited since that stamp.
    Transcripts, `reader@N` records, current stamps and records with no stamp are never upgrade re-reads."""
    if not rec.extracted_by or rec.extracted_by.rpartition("@")[0] != PARSER:
        return False
    if not extractor_stale(rec, versions):
        return False
    if rec.indexed_generation is not None:
        return bool(rec.content_version) and rec.indexed_version == rec.content_version
    return rec.dirty_since is None
