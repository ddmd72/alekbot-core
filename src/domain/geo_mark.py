"""Geo marks (GEO_MARKS_RFC §4.1, §4.2, §4.5): a position the owner shared on purpose, and the texts the model reads.

A mark is an event in the conversation, never a stored record: it exists only as history text. `GeoFix` is what an
entry point received (validated once, here), `GeoMark` adds the reverse-geocoded nearest address, and
`GeoLinkBinding` is the server state behind one Slack `$geo` link (stored under the link id's keyed digest; the raw id
is never stored or logged).

The rendered texts are descriptions, never imperatives: `render_mark_text` is persisted in a user message, and the
consolidator reads every user message as the owner's own words, so a rule there would become a standing directive.
`render_call_note` is a system item of a running call, never persisted, so it may state a rule.

The module holds several public names on purpose: the mark's values, their constants and their pure renderers.
"""
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from .local_time import zone_or_utc

LINK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")
LINK_DIGEST_PURPOSE = "geo_mark_link"
LINK_TTL_S = 600
PRECISE_ACCURACY_M = 100


class GeoSource(str, Enum):
    """The entry point a fix came from."""
    SLACK_LINK = "slack_geo"
    TELEGRAM_PIN = "telegram_pin"
    LELIK_BUTTON = "lelik_button"


def _number(value: Any, name: str) -> float:
    """A finite float from an int or a float; a bool, a string or anything else is refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} is not finite")
    return number


@dataclass(frozen=True)
class GeoFix:
    """A shared position: degrees, the fix's accuracy radius in metres (None when the platform sent none), the aware
    UTC instant it was shared, and its source. Built only through `parse`."""
    latitude: float
    longitude: float
    accuracy_m: Optional[float]
    shared_at: datetime
    source: GeoSource

    @staticmethod
    def parse(latitude: Any, longitude: Any, accuracy_m: Any, shared_at: datetime, source: GeoSource) -> "GeoFix":
        """The only validation the entry points need. ValueError: a non-finite or non-numeric value (a bool is not a
        number), latitude outside ±90, longitude outside ±180, a negative accuracy, a naive `shared_at`."""
        lat = _number(latitude, "latitude")
        lon = _number(longitude, "longitude")
        if not -90.0 <= lat <= 90.0:
            raise ValueError("latitude out of range")
        if not -180.0 <= lon <= 180.0:
            raise ValueError("longitude out of range")
        accuracy = None if accuracy_m is None else _number(accuracy_m, "accuracy")
        if accuracy is not None and accuracy < 0:
            raise ValueError("accuracy is negative")
        if shared_at.tzinfo is None or shared_at.utcoffset() is None:
            raise ValueError("shared_at has no time zone")
        return GeoFix(lat, lon, accuracy, shared_at.astimezone(timezone.utc), GeoSource(source))


@dataclass(frozen=True)
class GeoMark:
    """A fix and the nearest street address the lookup returned (None: no lookup, or it returned none)."""
    fix: GeoFix
    address: Optional[str]

    def to_wire(self) -> Dict[str, Any]:
        return {
            "latitude": self.fix.latitude, "longitude": self.fix.longitude, "accuracy_m": self.fix.accuracy_m,
            "shared_at": self.fix.shared_at.isoformat(), "source": self.fix.source.value, "address": self.address,
        }

    @staticmethod
    def from_wire(raw: Mapping[str, Any]) -> "GeoMark":
        """ValueError (or KeyError) on a missing or invalid field; the fix is validated as `GeoFix.parse` does."""
        address = raw.get("address")
        if address is not None and not isinstance(address, str):
            raise ValueError("address is not a string")
        fix = GeoFix.parse(raw["latitude"], raw["longitude"], raw.get("accuracy_m"),
                           datetime.fromisoformat(raw["shared_at"]), GeoSource(raw["source"]))
        return GeoMark(fix=fix, address=address)


_BINDING_FIELDS = ("user_id", "account_id", "platform", "channel_id", "session_id", "platform_user_id", "language",
                   "text")


@dataclass(frozen=True)
class GeoLinkBinding:
    """What one `$geo` link is bound to: whose mark, which channel and session it lands in, the platform user the
    queued turn re-authorizes, the owner's UI language, and the owner's text after `$geo` (empty: no question)."""
    user_id: str
    account_id: str
    platform: str
    channel_id: str
    session_id: str
    platform_user_id: str
    language: str
    text: str

    def to_record(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name in _BINDING_FIELDS}

    @staticmethod
    def from_record(raw: Mapping[str, Any]) -> "GeoLinkBinding":
        """ValueError on a missing or non-string field."""
        values = {}
        for name in _BINDING_FIELDS:
            value = raw.get(name)
            if not isinstance(value, str):
                raise ValueError(f"{name} is missing or not a string")
            values[name] = value
        return GeoLinkBinding(**values)


def accuracy_band(mark: GeoMark) -> str:
    """For logs only: "precise" (≤ PRECISE_ACCURACY_M), "approx" (above it) or "none" (no accuracy)."""
    accuracy = mark.fix.accuracy_m
    if accuracy is None:
        return "none"
    return "precise" if accuracy <= PRECISE_ACCURACY_M else "approx"


_SOURCE_PHRASES = {
    GeoSource.SLACK_LINK: "from the location link in Slack",
    GeoSource.TELEGRAM_PIN: "as a Telegram location",
    GeoSource.LELIK_BUTTON: "from the voice call page",
}

_MARK_CLOSING = (
    "This records the position the user shared at that moment, for what they ask with it or soon after it. It is not "
    "evidence of where they are at any later time; later, it shows only where they were at its 'Shared at' time. The "
    "user shares a position only when they choose to; with no recent mark in the conversation, the home location stays "
    "the reference."
)


def _map_point(fix: GeoFix) -> bool:
    """A Telegram pin without accuracy may be a point picked on the map (RFC §4.6); a browser fix never is."""
    return fix.accuracy_m is None and fix.source is GeoSource.TELEGRAM_PIN


def _position_phrase(fix: GeoFix) -> str:
    return "a point" if _map_point(fix) else "their position at that moment"


def _accuracy_clause(fix: GeoFix, who: str) -> str:
    accuracy = fix.accuracy_m
    if accuracy is None:
        if _map_point(fix):
            return f" — no accuracy came with it, so this may be a point picked on the map rather than where the {who} was"
        return " — no accuracy came with it, so how close it is to the spot is unknown"
    if accuracy <= PRECISE_ACCURACY_M:
        return f", accurate to about {round(accuracy)} m"
    km = math.floor(accuracy / 100 + 0.5) / 10      # half up: 250 m is 0.3 km
    return (f", approximate: within about {round(accuracy)} m (or {km:.1f} km), so the address names the area, not the "
            "exact spot")


def _address_clause(address: Optional[str], who: str) -> str:
    if address:
        return f"{address} (looked up from the position, not stated by the {who})"
    return "not available (the lookup returned no address)"


def _coordinates(fix: GeoFix) -> str:
    return f"{fix.latitude:.5f}, {fix.longitude:.5f}"


def local_shared_at(mark: GeoMark, zone: str) -> datetime:
    """The instant the mark was shared, on the owner's clock (`zone`; UTC when empty, unknown or malformed)."""
    return mark.fix.shared_at.astimezone(zone_or_utc(zone))


def _utc_offset(moment: datetime) -> str:
    offset = moment.strftime("%z")          # "+0200"
    return f"UTC{offset[:3]}:{offset[3:]}"


def render_mark_text(mark: GeoMark, zone: str) -> str:
    """The history text of a mark (RFC §4.2): a user-message part, `[Geo mark: …]`, never the owner's own words."""
    fix = mark.fix
    local = local_shared_at(mark, zone)
    return "\n".join([
        f"[Geo mark: the user shared {_position_phrase(fix)}, on purpose, {_SOURCE_PHRASES[fix.source]}]",
        f"Shared at: {local.strftime('%Y-%m-%d %H:%M')} ({_utc_offset(local)})",
        f"Position: {_coordinates(fix)}{_accuracy_clause(fix, 'user')}",
        f"Nearest street address: {_address_clause(mark.address, 'user')}",
        _MARK_CLOSING,
    ])


def render_call_note(mark: GeoMark) -> str:
    """The system note a running call receives (RFC §4.7): never persisted by us, so it may state a rule."""
    fix = mark.fix
    return (f"[The caller has just shared {_position_phrase(fix)}, on purpose, {_SOURCE_PHRASES[fix.source]}: "
            f"{_coordinates(fix)}{_accuracy_clause(fix, 'caller')}; nearest street address: "
            f"{_address_clause(mark.address, 'caller')}. They shared it for what they ask with it. Do not read the coordinates "
            "aloud.]")
