"""
The texts of the open bound (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §29.7). Code writes every one; the model only
reads them. Placeholders are filled by `drive_open_bound`; numbers carry thousands separators there. Pure.
"""
from __future__ import annotations

from typing import Dict

from .drive_text_extent import ExtentWhy

TRANSCRIPT_SUBJECT = "the transcript"

REFUSAL = ("{path} is too long to open whole: {chars} characters, {passages} passages in the index. "
           "A file this long is never delivered whole; nothing was downloaded.")
REFUSAL_TRUNCATED = ("{path} is too long to open whole: more than {chars} characters; the index holds its first "
                     "{passages} passages. A file this long is never delivered whole; nothing was downloaded.")

ROUTE_SPECIALIST = ("For what it says about something: find_passages with file_refs={ref}. Only when the need is its "
                    "text in order (its beginning, the next page, its full text): read_passages file_ref={ref} "
                    "passages=\"from 1\". Correcting this refused open is not a follow-up. If you opened it because its passages did not answer, or for a "
                    "total, report now.")
ROUTE_ORCHESTRATOR = ("To read it, commission work_with_drive_files for this file: the question, for what it says about "
                      "something; its beginning or a next page only when the user's own words ask for its text "
                      "in order.")

CUT_NOT_INDEXED = ("[Cut by the system: {subject} is {chars} characters; only the first {kept} are above. The rest "
                   "cannot be read now: the file has no indexed text ({why}).]")
CUT_STALE = ("[Cut by the system: {subject} is {chars} characters; only the first {kept} are above, read from the "
             "file as it is now. The rest of this version cannot be read until the index reads it again. The index "
             "still holds the version before the latest change ({passages} passages, marked stale): its passages can "
             "be searched by what they say, and they may not hold the change.]")
CUT_INDEXED = ("[Cut by the system: {subject} is {chars} characters; only the first {kept} are above. The rest can be "
               "read through the index, by what it says or a page at a time.]")

WHY: Dict[ExtentWhy, str] = {
    ExtentWhy.OFF: "drive indexing is off",
    ExtentWhy.NOT_CONNECTED: "the drive is not connected",
    ExtentWhy.REBUILDING: "the index is being rebuilt",
    ExtentWhy.NOT_READ: "the file is not read yet",
    ExtentWhy.NO_TEXT: "the index holds no text for it",
    ExtentWhy.HIDDEN: "it is in a folder the index does not show",
    ExtentWhy.UNKNOWN: "the index could not be asked",
}

STAND_IN_CUT = ("[{handle}: opened {label}, cut at {kept} of {chars} characters, delivered to the orchestrator; "
                "you do not see the text]")
STAND_IN_CUT_STALE_TAIL = (" For a later part by what it says: find_passages with file_refs={ref} (the version before "
                           "the latest change).")
