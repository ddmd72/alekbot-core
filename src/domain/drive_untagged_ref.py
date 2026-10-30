"""A `drive:` ref that carries no connection tag (MULTI_DRIVE_RFC §9, D13).

Refs written before the drives were reorganised (`drive:<item_id>`) parse to this. They are never resolved: no legacy
resolver, no history rewrite. The resolution service answers them with the "search again" line once, quoting `raw`.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class UntaggedRef:
    raw: str          # the ref as found in the text, `drive:` included, label punctuation excluded

    def __str__(self) -> str:
        return self.raw
