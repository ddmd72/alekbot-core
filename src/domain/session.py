from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict
import time

class Session(BaseModel):
    """
    Represents a conversation session.
    Maintains compatibility with FirestoreSessionStore while supporting sliding window.
    """
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(default="")
    user_id: str = Field(default="", alias="owner_id")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_activity: float = Field(default_factory=time.time)
    history: List[Any] = Field(default_factory=list, alias="messages")
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    # Consolidation tracking
    message_count: int = 0
    last_consolidation_at: Optional[float] = None

    @property
    def messages(self):
        return self.history
    
    @messages.setter
    def messages(self, value):
        self.history = value
        self.message_count = len(value)

    @property
    def owner_id(self):
        return self.user_id
    
    @owner_id.setter
    def owner_id(self, value):
        self.user_id = value

    def add_message(self, message: Any):
        self.history.append(message)
        self.message_count = len(self.history)
        self.updated_at = time.time()
        self.last_activity = self.updated_at

    def should_consolidate(self, threshold: int) -> bool:
        """Check if session needs consolidation."""
        return len(self.history) > threshold

    def extract_oldest_messages(self, count: int) -> List[Any]:
        """Extract and remove oldest messages."""
        batch = self.history[:count]
        self.history = self.history[count:]
        self.message_count = len(self.history)
        self.last_consolidation_at = time.time()
        return batch

# Alias for backward compatibility
SessionState = Session
