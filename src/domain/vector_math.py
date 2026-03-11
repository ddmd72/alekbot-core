"""
Domain utility for vector mathematics.

Pure mathematical functions with no dependencies on Infrastructure layer.
Centralized vector operations to avoid code duplication.

Session 2026-02-08: Smart Deduplication - Centralized Vector Math
- Replaces duplicated cosine_similarity implementations
- Used by: firestore_repo.py, search_enrichment_service.py
- numpy isolated in Domain (pure mathematical library)
"""

import numpy as np
from typing import List


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Calculate cosine similarity between two vectors.
    
    Formula: similarity = (A · B) / (||A|| × ||B||)
    
    Range: 0.0 to 1.0 (higher = more similar)
    
    This centralizes vector math to avoid duplication:
    - WRITE path: firestore_repo.add_fact_if_unique()
    - READ path: search_enrichment_service._deduplicate_semantic()
    
    Session 2026-02-08: Replaced scipy with numpy
    - scipy removed from requirements.txt (100MB reduction)
    - numpy already available, identical result
    - Works in all environments (local, Cloud Run, Docker)
    
    Args:
        vec1: First vector (embedding)
        vec2: Second vector (embedding)
        
    Returns:
        Cosine similarity (0.0 to 1.0)
        
    Example:
        >>> vec1 = [1.0, 0.0, 0.0]
        >>> vec2 = [1.0, 0.0, 0.0]
        >>> cosine_similarity(vec1, vec2)
        1.0  # Identical
        
        >>> vec1 = [1.0, 0.0, 0.0]
        >>> vec2 = [0.0, 1.0, 0.0]
        >>> cosine_similarity(vec1, vec2)
        0.0  # Orthogonal
    
    Note:
        Identical result to: 1.0 - scipy.spatial.distance.cosine(vec1, vec2)
    """
    dot_product = np.dot(vec1, vec2)
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)
    
    # Avoid division by zero
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return dot_product / (norm1 * norm2)
