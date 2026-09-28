from typing import NamedTuple


class VoiceDelegationOutcome(NamedTuple):
    """One delegation from a live call: the text Lelik speaks, and whether it is a failure
    (rejection, exception, max retries). A failure's text is an error string, never an answer:
    it may be spoken as "it did not go through" but must never be posted to chat verbatim."""

    text: str
    failed: bool
