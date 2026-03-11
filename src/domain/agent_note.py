"""
Agent Note domain model.

A short-lived contextual annotation written by the orchestrator to itself.
Injected into subsequent prompt turns by Router enrichment.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class AgentNote:
    """Persisted orchestrator note, injected into future prompts."""
    note_id: str
    user_id: str
    text: str
    created_at: datetime
    visible_after: Optional[datetime] = None   # None = visible immediately
    expires_after: Optional[datetime] = None   # None = never expires


@dataclass
class NoteCreate:
    """Input for creating a new note."""
    user_id: str
    text: str
    visible_after: Optional[datetime] = None
    expires_after: Optional[datetime] = None


@dataclass
class NoteUpdate:
    """Input for updating an existing note."""
    note_id: str
    user_id: str
    text: Optional[str] = None
    visible_after: Optional[datetime] = None
    expires_after: Optional[datetime] = None
