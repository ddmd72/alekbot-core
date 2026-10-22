from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import time
import uuid

# A FAILED batch is revived by the hourly sweep at most this many times in its whole life
# (3 attempts per round, so 4 x 3 = 12 paid attempts at most). Never reset.
MAX_FAILED_REVIVALS = 3


class BatchStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    RETRY_PENDING = "retry_pending"
    FAILED = "failed"

class ConsolidationBatch(BaseModel):
    batch_id: str = Field(default_factory=lambda: f"batch_{uuid.uuid4().hex[:12]}")
    user_id: str
    session_id: str
    messages: List[Dict[str, Any]] = Field(default_factory=list)  # Serialized MessageContext
    created_at: float = Field(default_factory=time.time)
    status: BatchStatus = BatchStatus.PENDING
    attempts: int = 0
    last_error: Optional[str] = None
    facts_extracted: int = 0
    processed_at: Optional[float] = None
    # Stamped when the batch enters PROCESSING. Used by reset_recoverable_batches
    # to distinguish a live run from a zombie: a batch can only be legitimately
    # PROCESSING while its Cloud Task is alive (dispatch_deadline = 1800s).
    processing_started_at: Optional[float] = None
    # How many times the sweep revived this batch from FAILED. Lifetime counter, never cleared
    # by a reset; caps the paid retries (MAX_FAILED_REVIVALS).
    revivals: int = 0
