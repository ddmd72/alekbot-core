"""
Core LLM conversation types.

These are fundamental domain types for the conversational system — not
implementation details of any specific LLM provider.

Moved from ports/llm_service.py to eliminate port→port dependencies.
"""

import time
from typing import List, Any, Optional, Dict
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    name: str
    args: Dict[str, Any]
    thought_signature: Optional[str] = None


class MessagePart(BaseModel):
    text: Optional[str] = None
    full_text: Optional[str] = None  # Full response (when text=summary). Used for tiered history loading.
    tool_call: Optional[ToolCall] = None
    tool_response: Optional[Dict[str, Any]] = None  # {name: str, response: Any}
    file_data: Optional[Dict[str, Any]] = None  # {uri: str, mime_type: str}


class Message(BaseModel):
    role: str  # "user", "model", "system"
    parts: List[MessagePart]
    raw_content: Any = None
    created_at: float = Field(default_factory=time.time)
