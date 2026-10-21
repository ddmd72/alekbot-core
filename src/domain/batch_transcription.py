"""
Value types of the batch transcription contract (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.5).

Waiver of "one public class per file": these are the several value types of one contract (`BatchTranscriptionPort`),
the precedent of `drive_enrichment.py`. Pure data: stdlib and the billing value type only.

A stored job keeps the provider file *name* (`files/<id>`), never the URI: the adapter builds the URI from the name
when it builds a request, so a URI is never persisted or logged.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from src.domain.billing import BatchUsage


class BatchStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class FileRef:
    """An uploaded input file: the provider name is the durable handle, the expiry is when the provider drops it."""
    name: str
    uri: str
    expires_at: datetime


@dataclass(frozen=True)
class BatchRef:
    """A submitted batch job. `keys` are the request keys in submission order; `collect` checks each answer's key
    against them instead of trusting order."""
    name: str
    keys: Tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchRequest:
    """One request of a batch. `file_name` (+ `file_mime`) references an uploaded file by its stored name; empty for a
    text-only request. `response_schema` is a JSON-schema-style mapping (lowercase types); the adapter makes it the
    provider's typed schema."""
    key: str
    system_instruction: str
    user_text: str
    response_schema: Optional[Mapping[str, Any]] = None
    file_name: str = ""
    file_mime: str = ""
    thinking_level: str = "low"
    max_output_tokens: Optional[int] = None


@dataclass(frozen=True)
class BatchResult:
    """The outcome of one request: `text` on success, else `error`. `refused` marks a provider refusal of this
    request's input (content policy), which the caller reads as a gap, not as a retryable failure. For a response
    that held its own provider error, `code` is that error's rpc code (0 when none) and `transient` says the code
    is one a later attempt can clear (4, 8, 13, 14); the caller decides what to charge."""
    key: str
    text: Optional[str] = None
    error: Optional[str] = None
    refused: bool = False
    usage: Optional[BatchUsage] = None
    finish_reason: str = ""
    code: int = 0
    transient: bool = False
