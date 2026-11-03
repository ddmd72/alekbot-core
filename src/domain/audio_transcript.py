"""
One transcription with what it cost the provider (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4 P21, "Metered"). Pure.

A transcribing open bills each call to the account like the reader's calls, with its model and its usage. Providers
report usage in one of two shapes: tokens (input and output) or audio seconds; the shape absent stays zero, and a
response with no usage reports zeros everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioTranscript:
    text: str
    model: str                   # the model that transcribed, as the adapter sent it
    input_tokens: int = 0        # token-shaped usage
    output_tokens: int = 0
    seconds: float = 0.0         # duration-shaped usage
