from google import genai
import asyncio
from typing import List
from ..ports.embedding_service import EmbeddingService

class GeminiEmbeddingAdapter(EmbeddingService):
    """Adapter: Gemini-specific embedding implementation."""
    
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiEmbeddingAdapter")
        self.api_key = api_key
        # Ensure we pass api_key correctly to the client
        self.client = genai.Client(api_key=api_key)
    
    async def get_embedding(
        self,
        text: str,
        task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> List[float]:
        """Generate 768-dimensional embedding via Gemini API."""
        result = await asyncio.to_thread(
            self.client.models.embed_content,
            model="models/gemini-embedding-001",
            contents=text,
            config={
                "task_type": task_type,
                "output_dimensionality": 768
            }
        )
        return result.embeddings[0].values

    async def get_embeddings_batch(
        self,
        texts: List[str],
        task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> List[List[float]]:
        """Generate 768-dimensional embeddings for multiple texts in a single API call."""
        result = await asyncio.to_thread(
            self.client.models.embed_content,
            model="models/gemini-embedding-001",
            contents=texts,
            config={
                "task_type": task_type,
                "output_dimensionality": 768
            }
        )
        return [e.values for e in result.embeddings]
