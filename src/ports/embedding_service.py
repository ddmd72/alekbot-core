from abc import ABC, abstractmethod
from typing import List

class EmbeddingService(ABC):
    """Port: Text embedding generation service."""
    
    @abstractmethod
    async def get_embedding(
        self, 
        text: str, 
        task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> List[float]:
        """Generate embedding vector for text."""
        pass
