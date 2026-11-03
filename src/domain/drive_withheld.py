"""Why an open withheld a media file before any download (the drive connector's media pre-check).

`DriveOperations.open(..., withhold_media=True)` answers these on the item's metadata alone, so nothing is downloaded and
nothing is posted to a chat; the outcome carries the code in `DriveOutcome.withheld` and no wording: the consumer (the
drive connector) composes its own line. The chat never sets the flag, so a chat outcome never carries a code.
"""
from __future__ import annotations

from enum import Enum


class Withheld(str, Enum):
    """`VIDEO`: a video file, which an open would otherwise send to the owner's chat as a file. `RECORDING_UNTRANSCRIBED`:
    a recording with no stored transcript, which an open would otherwise download only to find it cannot be read."""
    VIDEO = "video"
    RECORDING_UNTRANSCRIBED = "recording_untranscribed"
