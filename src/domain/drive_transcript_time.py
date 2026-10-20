"""
Transcript stamps (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.5, Δ128). The model writes `MM:SS` (or `H:MM:SS` from one
hour) strings, never integer seconds: T0 measured a ~40 % stretch of integer-second times and ~0.1 s drift with strings.
Pure: no clock, no I/O.
"""
import re

_MM_SS = re.compile(r"^([0-9]{1,2}):([0-9]{2})$")
_H_MM_SS = re.compile(r"^([0-9]{1,2}):([0-9]{2}):([0-9]{2})$")


def parse_stamp(text: str) -> float:
    """`M:SS`, `MM:SS` or `H:MM:SS` to seconds. Minutes and seconds are 0-59; anything else raises ValueError."""
    s = text.strip() if isinstance(text, str) else ""
    m = _H_MM_SS.match(s)
    if m:
        hours, minutes, seconds = (int(g) for g in m.groups())
    else:
        m = _MM_SS.match(s)
        if not m:
            raise ValueError("not a transcript stamp")
        hours = 0
        minutes, seconds = (int(g) for g in m.groups())
    if minutes > 59 or seconds > 59:
        raise ValueError("stamp minutes and seconds must be 0-59")
    return float(hours * 3600 + minutes * 60 + seconds)


def format_stamp(seconds: float) -> str:
    """Seconds (floored) to `MM:SS` below one hour, `H:MM:SS` from one hour."""
    if seconds < 0:
        raise ValueError("negative stamp")
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
