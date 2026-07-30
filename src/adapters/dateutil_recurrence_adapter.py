"""
DateutilRecurrenceAdapter — RecurrencePort over ``dateutil.rrule``.

Recurrence is evaluated on the user's LOCAL wall clock and converted back to UTC,
so "every day at 09:00" stays 09:00 across a DST transition. The anchor is always
the reminder's current ``due``; rules carry no ``DTSTART`` of their own.

Every expansion is bounded by ``_HORIZON_YEARS``: an RRULE that never matches
(``FREQ=MONTHLY;BYMONTHDAY=31;BYMONTH=2``) would otherwise make ``rrule.after()``
iterate without end. The bound is applied by handing dateutil a probe copy of the
rule with an ``UNTIL`` — beyond the horizon the answer is ``None``, never a hang.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from ..ports.recurrence_port import RecurrencePort
from ..utils.logger import logger

# How far ahead an expansion may look. Ten years covers any realistic reminder
# ("every year on 1 Jan") while keeping an unmatchable rule terminating.
_HORIZON_YEARS = 10

_ALLOWED_FREQ = {"HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
# Sub-hourly frequencies are meaningless here: the firing cron ticks every 15 min.
_REJECTED_PARTS = ("DTSTART", "EXDATE", "RDATE")

_FREQ_UNIT = {
    "HOURLY": "hour",
    "DAILY": "day",
    "WEEKLY": "week",
    "MONTHLY": "month",
    "YEARLY": "year",
}
_DAY_NAMES = {
    "MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu",
    "FR": "Fri", "SA": "Sat", "SU": "Sun",
}
_ORDINALS = {"1": "1st", "2": "2nd", "3": "3rd", "4": "4th", "-1": "last", "-2": "2nd-to-last"}


class DateutilRecurrenceAdapter(RecurrencePort):

    # ------------------------------------------------------------------ #
    # Validation                                                         #
    # ------------------------------------------------------------------ #

    def normalize(self, rule: str) -> str:
        parts = self._parse(rule)

        freq = parts.get("FREQ")
        if freq not in _ALLOWED_FREQ:
            raise ValueError(
                f"FREQ must be one of {', '.join(sorted(_ALLOWED_FREQ))} "
                f"(got {freq or 'nothing'})."
            )
        if "COUNT" in parts or "UNTIL" in parts:
            raise ValueError(
                "COUNT/UNTIL are not supported — a reminder ends by being deleted, "
                "not by expiring into a document that never fires again."
            )

        canonical = ";".join(f"{k}={v}" for k, v in parts.items())

        # Probe from now: a rule that yields nothing in ten years is unusable.
        probe_start = datetime.now()
        if self._expand(canonical, probe_start, inc=True) is None:
            raise ValueError(
                f"this rule never occurs within {_HORIZON_YEARS} years "
                "— check BYMONTHDAY/BYMONTH combinations."
            )
        return canonical

    # ------------------------------------------------------------------ #
    # Expansion                                                          #
    # ------------------------------------------------------------------ #

    def first_occurrence(
        self, rule: str, not_before: datetime, tz: str
    ) -> Optional[datetime]:
        return self._occurrence(rule, not_before, tz, inc=True)

    def next_occurrence(self, rule: str, after: datetime, tz: str) -> Optional[datetime]:
        return self._occurrence(rule, after, tz, inc=False)

    def _occurrence(
        self, rule: str, anchor: datetime, tz: str, inc: bool
    ) -> Optional[datetime]:
        zone = self._zone(tz)
        # Local wall clock in, local wall clock out — the whole point of evaluating
        # in the user's zone rather than UTC.
        local_anchor = anchor.astimezone(zone).replace(tzinfo=None)
        try:
            occurrence = self._expand(rule, local_anchor, inc=inc)
        except ValueError as exc:
            logger.warning("⚠️ [Recurrence] Unusable rule %r: %s", rule, exc)
            return None
        if occurrence is None:
            return None
        return occurrence.replace(tzinfo=zone).astimezone(timezone.utc)

    def _expand(self, rule: str, local_anchor: datetime, inc: bool) -> Optional[datetime]:
        """First match at/after ``local_anchor``, or None past the horizon.

        ``UNTIL`` is appended to the copy handed to dateutil — never to the stored
        rule — purely to bound the search.
        """
        horizon = local_anchor + timedelta(days=365 * _HORIZON_YEARS)
        bounded = f"RRULE:{rule};UNTIL={horizon.strftime('%Y%m%dT%H%M%S')}"
        try:
            expansion = rrulestr(bounded, dtstart=local_anchor)
        except Exception as exc:  # dateutil raises bare ValueError/TypeError variants
            raise ValueError(str(exc)) from exc
        return expansion.after(local_anchor, inc=inc)

    # ------------------------------------------------------------------ #
    # Description                                                        #
    # ------------------------------------------------------------------ #

    def describe(self, rule: str) -> str:
        try:
            parts = self._parse(rule)
        except ValueError:
            return rule
        unit = _FREQ_UNIT.get(parts.get("FREQ", ""))
        if not unit:
            return rule

        interval = parts.get("INTERVAL", "1")
        phrase = [f"every {unit}" if interval == "1" else f"every {interval} {unit}s"]

        if byday := parts.get("BYDAY"):
            phrase.append("on " + ", ".join(self._day_label(d) for d in byday.split(",")))
        if bymonthday := parts.get("BYMONTHDAY"):
            phrase.append("on day " + ", ".join(bymonthday.split(",")))
        if byhour := parts.get("BYHOUR"):
            minute = parts.get("BYMINUTE", "0").split(",")[0]
            try:
                phrase.append(
                    "at " + ", ".join(
                        f"{int(h):02d}:{int(minute):02d}" for h in byhour.split(",")
                    )
                )
            except ValueError:
                return rule
        return " ".join(phrase)

    @staticmethod
    def _day_label(token: str) -> str:
        """``TU`` → Tue; ``-1SU`` → last Sun; ``2MO`` → 2nd Mon."""
        day = _DAY_NAMES.get(token[-2:])
        if not day:
            return token
        prefix = token[:-2]
        if not prefix:
            return day
        return f"{_ORDINALS.get(prefix, prefix)} {day}"

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse(rule: str) -> Dict[str, str]:
        """Rule text → ordered {KEY: VALUE}. Raises ValueError on anything unusable."""
        text = (rule or "").strip().upper()
        if not text:
            raise ValueError("recurrence rule is empty.")
        if "\n" in text or "\r" in text:
            raise ValueError("expected a single RRULE line.")
        if text.startswith("RRULE:"):
            text = text[len("RRULE:"):].strip()
        for banned in _REJECTED_PARTS:
            if banned in text:
                raise ValueError(
                    f"{banned} is not accepted — the reminder's own due date is the anchor."
                )
        parts: Dict[str, str] = {}
        for chunk in text.split(";"):
            if not chunk.strip():
                continue
            if "=" not in chunk:
                raise ValueError(f"malformed segment {chunk!r}; expected KEY=VALUE.")
            key, value = chunk.split("=", 1)
            parts[key.strip()] = value.strip()
        return parts

    @staticmethod
    def _zone(tz: str) -> ZoneInfo:
        try:
            return ZoneInfo(tz) if tz else ZoneInfo("UTC")
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning("⚠️ [Recurrence] Unknown timezone %r — using UTC", tz)
            return ZoneInfo("UTC")
