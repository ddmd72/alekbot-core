"""Why an open withheld a file's content (the drive connector's media pre-check and its no-paid-reader rule).

`DriveOperations.open(..., withhold_media=True)` answers a recording (audio or video) and an image on the item's metadata
alone, so nothing is downloaded and nothing is posted to a chat; a PDF is parsed and never handed to the paid model
reader. The outcome carries the code in `DriveOutcome.withheld` and no wording: the consumer (the drive connector)
composes its own line. The chat never sets the flag, so a chat outcome never carries a code.
"""
from __future__ import annotations

from enum import Enum


class Withheld(str, Enum):
    """`RECORDING_UNTRANSCRIBED`: a recording (audio, or a video: every video is a recording kind) with no stored
    transcript, which an open would otherwise download only to find it cannot be read, or post to the chat (a video).
    `IMAGE`: an image, which only the paid model reader can read (answered before any download). `SCAN`: a PDF the
    parser read and the router would hand to the paid model reader (a scan, a thin or garbled text layer); the reader
    did not run (`document_read.CALLER_CONNECTOR`). A ref that is not a `drive:` ref carries no code: the connector's
    facade refuses it with the plain neutral refusal (`DriveReadOperations.open`, class REFUSED)."""
    RECORDING_UNTRANSCRIBED = "recording_untranscribed"
    IMAGE = "image"
    SCAN = "scan"
