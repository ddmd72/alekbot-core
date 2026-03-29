"""
Smart Deduplication Service
===========================

Service for intelligent fact deduplication with number-aware comparison.

Session: 2026-02-08 Deduplication Strategy Optimization
Purpose: Prevent loss of valuable facts while filtering true duplicates.

Philosophy: "Better to add a duplicate than to lose important information"

Algorithm:
1. similarity < 0.96 → NOT duplicate
2. Numbers differ (sorted) → NOT duplicate (early exit)
3. similarity < 0.98 AND existing shorter by 15%+ → NOT duplicate
4. Otherwise → DUPLICATE

Author: Session 2026-02-08
"""

import re
from typing import List, Tuple


class SmartDeduplication:
    """
    Intelligent deduplication for LLM-generated facts.
    
    Key features:
    - Number-aware comparison (sorted to ignore order)
    - Length-based heuristic for detail preservation
    - Multi-level similarity thresholds (configurable)
    - Early exit optimization
    
    Thresholds:
    - moderate_threshold: Quick exit for dissimilar (default: 0.96)
    - strict_threshold: Very similar → duplicate (default: 0.98, configurable)
    - LENGTH_RATIO = 0.85 (existing must be 85%+ of new to be duplicate)
    
    Session 2026-02-16: Made thresholds configurable for consolidation search
    - Default: 0.96/0.98 (balanced for READ/WRITE)
    - Consolidation: 0.96/1.0 (only exact duplicates)
    """
    
    # Length ratio threshold (existing/new)
    LENGTH_RATIO_THRESHOLD = 0.85  # 15% difference
    
    def __init__(
        self,
        moderate_threshold: float = 0.96,
        strict_threshold: float = 0.98
    ):
        """
        Initialize smart deduplication service with configurable thresholds.
        
        Args:
            moderate_threshold: Below this → NOT duplicate (default: 0.96)
            strict_threshold: Above this → DUPLICATE (default: 0.98)
                - 0.98: Default READ/WRITE (balanced filtering)
                - 1.0: Only exact duplicates (consolidation search mode)
        """
        self.moderate_threshold = moderate_threshold
        self.strict_threshold = strict_threshold
    
    def is_duplicate(
        self,
        new_text: str,
        existing_text: str,
        similarity: float
    ) -> Tuple[bool, str]:
        """
        Determine if new fact is a duplicate of existing fact.
        
        Returns early (NOT duplicate) when:
        - similarity < 0.96
        - Numbers differ (sorted comparison)
        - similarity < 0.98 AND new fact has more detail
        
        Args:
            new_text: Text of new fact to add
            existing_text: Text of existing fact in database
            similarity: Cosine similarity (0.0 to 1.0)
            
        Returns:
            Tuple of (is_duplicate: bool, reason: str)
            - True, reason → DUPLICATE (reject new fact)
            - False, reason → NOT duplicate (add new fact)
            
        Examples:
            >>> service = SmartDeduplication()
            
            # Different values
            >>> service.is_duplicate(
            ...     "Weight 84 kg",
            ...     "Weight 75 kg",
            ...     0.97
            ... )
            (False, "numbers_differ: [83.0] != [84.0]")
            
            # More detail
            >>> service.is_duplicate(
            ...     "Weight 75 kg in Example City",
            ...     "Weight 75 kg",
            ...     0.96
            ... )
            (False, "new_more_detailed: existing 85% shorter")
            
            # True duplicate
            >>> service.is_duplicate(
            ...     "Weight was 75 kg",
            ...     "Weight 75 kg",
            ...     0.99
            ... )
            (True, "strict_similarity: 0.99 >= 0.98")
        """
        
        # 1️⃣ LEVEL 1: Quick exit for dissimilar facts
        if similarity < self.moderate_threshold:
            return False, f"low_similarity: {similarity:.3f} < {self.moderate_threshold}"
        
        # 2️⃣ LEVEL 2: Number comparison (highest priority)
        new_numbers = self._extract_and_sort_numbers(new_text)
        existing_numbers = self._extract_and_sort_numbers(existing_text)
        
        # If either fact has numbers, compare them
        if new_numbers or existing_numbers:
            if new_numbers != existing_numbers:
                # Numbers differ → NOT duplicate (early exit)
                return False, f"numbers_differ: {existing_numbers} != {new_numbers}"
        
        # 3️⃣ LEVEL 3: Very high similarity → duplicate
        if similarity >= self.strict_threshold:
            return True, f"strict_similarity: {similarity:.3f} >= {self.strict_threshold}"
        
        # 4️⃣ LEVEL 4: Length-based heuristic (for moderate similarity)
        # If new fact is significantly longer → might be more detailed
        if similarity < self.strict_threshold:
            length_ratio = len(existing_text) / len(new_text) if len(new_text) > 0 else 1.0
            
            if length_ratio < self.LENGTH_RATIO_THRESHOLD:
                # New fact is longer → likely more detailed → NOT duplicate
                return False, f"new_more_detailed: existing {length_ratio:.2f} < {self.LENGTH_RATIO_THRESHOLD}"
        
        # 5️⃣ DEFAULT: If all checks passed → consider duplicate
        return True, f"moderate_duplicate: similarity={similarity:.3f}, similar_length"
    
    def _extract_and_sort_numbers(self, text: str) -> List[float]:
        """
        Extract all numbers from text and return sorted list.
        
        Sorting allows order-independent comparison:
        "75 kg, 185 cm" == "185 cm, 75 kg"
        
        Handles:
        - Integers: "83"
        - Floats: "5.1"
        - Dates: "2025-03-28" → [2025, 3, 28]
        - Ranges: "95-98 kg" → [95, 98]
        
        Args:
            text: Input text
            
        Returns:
            Sorted list of numbers found in text
            
        Examples:
            >>> self._extract_and_sort_numbers("Weight 83.5 kg")
            [83.5]
            
            >>> self._extract_and_sort_numbers("2025-03-28")
            [3.0, 28.0, 2025.0]
            
            >>> self._extract_and_sort_numbers("Range 95-98 kg")
            [95.0, 98.0]
            
            >>> self._extract_and_sort_numbers("No numbers here")
            []
        """
        # Regex to match integers and floats
        pattern = r'\d+\.?\d*'
        matches = re.findall(pattern, text)
        
        # Convert to floats and sort
        numbers = [float(match) for match in matches]
        return sorted(numbers)


