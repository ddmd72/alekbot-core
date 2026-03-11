from enum import Enum
from typing import List, Optional, Dict
from pydantic import BaseModel, Field
import time
import uuid

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
    messages: List[Dict] = Field(default_factory=list)  # Serialized MessageContext
    created_at: float = Field(default_factory=time.time)
    status: BatchStatus = BatchStatus.PENDING
    attempts: int = 0
    last_error: Optional[str] = None
    facts_extracted: int = 0
    processed_at: Optional[float] = None
