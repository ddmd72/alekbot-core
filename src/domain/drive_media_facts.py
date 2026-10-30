"""Typed media facts of a drive item, filled by the adapter (MULTI_DRIVE_RFC §7.2).

They replace the raw provider facet mapping. Every field has a neutral default meaning "the provider said nothing";
the adapter parses and converts (a timestamp to an aware datetime, a duration to milliseconds) so no reader parses a
provider shape. The fields are the ones the readers use: the card, the capture rule, the triage hint, the effective
date, the planner's place step and the admission rules.

- `taken_at`: when a photo was taken (aware datetime), None when unknown.
- `camera_make`, `camera_model`: the camera the provider read from the file's metadata; "" when none.
- `latitude`, `longitude`: where a photo was taken; both or neither.
- `image_width`, `image_height`: pixel size of an image; 0 when unknown.
- `video_duration_ms`, `video_width`, `video_height`: a video's length and frame size; 0 when unknown.
- `audio_duration_ms`: an audio file's length; 0 when unknown.
- `package`: the item is a provider package (a bundle the provider shows as one file); never downloaded.
- `malware`: the provider flagged the item as malware; never downloaded.
- `alias_of`: the item is a pointer to another item (a shared item added to this drive, a shortcut): the identity of the
  item it points to, "" when the provider does not say which. None for an ordinary item. Admission refuses an alias.

Stored as the map `to_dict()` (one key per field, `taken_at` a timestamp); `from_dict(value)` reads it back, taking the
default for every missing key, so a record stored before a field existed reads with that field's default and a missing
or empty map reads as `DriveMediaFacts()`. Unknown keys are ignored.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class DriveMediaFacts:
    taken_at: Optional[datetime] = None
    camera_make: str = ""
    camera_model: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    image_width: int = 0
    image_height: int = 0
    video_duration_ms: int = 0
    video_width: int = 0
    video_height: int = 0
    audio_duration_ms: int = 0
    package: bool = False
    malware: bool = False
    alias_of: Optional[str] = None

    def __post_init__(self) -> None:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("a location has both a latitude and a longitude, or neither")
        if self.taken_at is not None and self.taken_at.tzinfo is None:
            raise ValueError("taken_at must be timezone-aware")
        for name in ("image_width", "image_height", "video_duration_ms", "video_width", "video_height",
                     "audio_duration_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} is not negative: {getattr(self, name)}")

    @property
    def location(self) -> Optional[Tuple[float, float]]:
        """(latitude, longitude), None when the provider gave no location."""
        if self.latitude is None or self.longitude is None:
            return None
        return self.latitude, self.longitude

    @property
    def is_alias(self) -> bool:
        return self.alias_of is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Optional[Mapping[str, Any]]) -> DriveMediaFacts:
        if not value:
            return cls()
        return cls(**{f.name: value[f.name] for f in fields(cls) if f.name in value})

