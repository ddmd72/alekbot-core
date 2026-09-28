from dataclasses import dataclass


@dataclass(frozen=True)
class CallerLeg:
    """The caller's side of a media room after the first offer/answer."""
    session_id: str
    answer_sdp: str
