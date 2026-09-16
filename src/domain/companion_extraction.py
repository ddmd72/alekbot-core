"""
CompanionExtractionBatch — session-scoped analog of ConsolidationBatch.

Mirrors ConsolidationBatch (src/domain/consolidation.py) but keyed by
session_id + account_id, never user_id, per the RFC's session-keyed
identity model (docs/10_rfcs/COMPANION_AGENTS_RFC.md §2, §6). companion_type
selects which per-companion-type extractor processes the batch (RFC §6:
"one prompt/agent per companion type" — "tutor" is the only value today).

BatchStatus is reused from domain/consolidation.py as-is: a plain status
enum (PENDING/PROCESSING/COMPLETED/RETRY_PENDING/FAILED) carries no
identity-model coupling, unlike FactRepository — RFC §11's rejection of
reuse does not apply to it.
"""
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
import time
import uuid

from .consolidation import BatchStatus

EXTRACTION_TASK = "extract_companion_batch"


class CompanionExtractionBatch(BaseModel):
    batch_id: str = Field(default_factory=lambda: f"cbatch_{uuid.uuid4().hex[:12]}")
    session_id: str
    account_id: str
    companion_type: str
    created_by_user_id: str
    messages: List[Dict] = Field(default_factory=list)  # Serialized MessageContext
    created_at: float = Field(default_factory=time.time)
    status: BatchStatus = BatchStatus.PENDING
    attempts: int = 0
    last_error: Optional[str] = None
    records_extracted: int = 0
    processed_at: Optional[float] = None
    # Stamped when the batch enters PROCESSING — same zombie-detection use as
    # ConsolidationBatch.processing_started_at.
    processing_started_at: Optional[float] = None
