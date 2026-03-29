"""
Domain utility for vector mathematics.

Pure mathematical functions with no dependencies on Infrastructure layer.
Centralized vector operations to avoid code duplication.

Session 2026-02-08: Smart Deduplication - Centralized Vector Math
- Replaces duplicated cosine_similarity implementations
- Used by: firestore_repo.py, search_enrichment_service.py
- numpy isolated in Domain (pure mathematical library)
"""

import math
from typing import List


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Calculate cosine similarity between two vectors.

    Formula: similarity = (A · B) / (||A|| × ||B||)

    Range: 0.0 to 1.0 (higher = more similar)

    This centralizes vector math to avoid duplication:
    - WRITE path: firestore_repo.add_fact_if_unique()
    - READ path: search_enrichment_service._deduplicate_semantic()

    Args:
        vec1: First vector (embedding)
        vec2: Second vector (embedding)

    Returns:
        Cosine similarity (0.0 to 1.0)
    """
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)
