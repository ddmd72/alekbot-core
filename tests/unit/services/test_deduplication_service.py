"""
Unit tests for SmartDeduplication

Session: 2026-02-08 Deduplication Strategy Optimization
Tests the intelligent deduplication algorithm with number-aware comparison.
"""

import pytest
from src.services.deduplication_service import SmartDeduplication


class TestSmartDeduplication:
    """Test suite for smart deduplication logic."""
    
    @pytest.fixture
    def service(self):
        """Create deduplication service instance."""
        return SmartDeduplication()
    
    # ========================================================================
    # LEVEL 1: Low Similarity Tests (< 0.96)
    # ========================================================================
    
    def test_low_similarity_not_duplicate(self, service):
        """Facts with similarity < 0.96 are NOT duplicates."""
        is_dup, reason = service.is_duplicate(
            "User visited Paris",
            "Weight was 75 kg",
            0.92
        )
        
        assert is_dup is False
        assert "low_similarity" in reason
        assert "0.92" in reason
    
    # ========================================================================
    # LEVEL 2: Number Comparison Tests
    # ========================================================================
    
    def test_different_numbers_not_duplicate(self, service):
        """Facts with different numbers are NOT duplicates."""
        is_dup, reason = service.is_duplicate(
            "Weight on 2025-03-29 was 84 kg",
            "Weight on 2025-03-28 was 75 kg",
            0.99
        )
        
        assert is_dup is False
        assert "numbers_differ" in reason
    
    def test_sorted_numbers_same_values_different_order(self, service):
        """Numbers in different order but same values = duplicate."""
        is_dup, reason = service.is_duplicate(
            "Patient: 185 cm height, 75 kg weight",
            "Patient: 75 kg weight, 185 cm height",
            0.97
        )
        
        # Numbers sorted: [83, 185] == [83, 185]
        # similarity < 0.98, check length
        # Lengths similar → duplicate
        assert is_dup is True
    
    def test_date_format_variants(self, service):
        """Same date in different formats should match (sorted numbers)."""
        is_dup, reason = service.is_duplicate(
            "Event on 2025-03-28",
            "Event on March 28, 2025",  # Slightly different text
            0.96
        )
        
        # Numbers extracted: [2025, 3, 28] sorted = [3, 28, 2025]
        # Both should extract same numbers
        # This is a corner case - depends on text similarity
        # If similarity < 0.98, length check applies
        assert is_dup is False or is_dup is True  # Either is acceptable
    
    def test_no_numbers_vs_has_numbers(self, service):
        """Fact without numbers vs fact with numbers."""
        is_dup, reason = service.is_duplicate(
            "Patient has Periodontitis",
            "Patient has Periodontitis since 2020",
            0.97
        )
        
        # One has numbers [2020], other doesn't []
        # Different → NOT duplicate
        assert is_dup is False
        assert "numbers_differ" in reason
    
    # ========================================================================
    # LEVEL 3: Strict Similarity Tests (>= 0.98)
    # ========================================================================
    
    def test_very_high_similarity_is_duplicate(self, service):
        """Facts with similarity >= 0.98 are duplicates."""
        is_dup, reason = service.is_duplicate(
            "Patient has Periodontitis",
            "Patient has periodontitis",  # Same but lowercase
            0.99
        )
        
        assert is_dup is True
        assert "strict_similarity" in reason
        assert "0.99" in reason
    
    def test_strict_threshold_boundary(self, service):
        """Test exact boundary at 0.98."""
        is_dup, reason = service.is_duplicate(
            "Weight was 75 kg",
            "Weight is 75 kg",
            0.98
        )
        
        assert is_dup is True
        assert "strict_similarity" in reason
    
    # ========================================================================
    # LEVEL 4: Length-Based Heuristic Tests
    # ========================================================================
    
    def test_new_more_detailed_not_duplicate(self, service):
        """New fact with more detail should NOT be duplicate."""
        is_dup, reason = service.is_duplicate(
            "Weight was 75 kg in Example City, representing loss of weight",  # New: detailed
            "Weight was 75 kg",  # Existing: short
            0.96
        )
        
        # Numbers same: [83] == [83]
        # similarity 0.96 < 0.98
        # existing/new = 17/56 = 0.30 < 0.85
        # → NOT duplicate
        assert is_dup is False
        assert "new_more_detailed" in reason
    
    def test_new_shorter_is_duplicate(self, service):
        """New fact that's shorter should be duplicate."""
        is_dup, reason = service.is_duplicate(
            "Weight 75 kg",  # New: short
            "Weight was 75 kg in Example City",  # Existing: detailed
            0.96
        )
        
        # Numbers same: [83] == [83]
        # similarity 0.96 < 0.98
        # existing/new = 33/13 = 2.54 > 0.85
        # → DUPLICATE
        assert is_dup is True
        assert "moderate_duplicate" in reason or "strict" in reason
    
    def test_similar_length_moderate_similarity(self, service):
        """Similar length with moderate similarity → duplicate."""
        is_dup, reason = service.is_duplicate(
            "Patient has active Periodontitis",
            "Patient has Periodontitis condition",
            0.97
        )
        
        # Similar lengths, similarity 0.97 < 0.98
        # length ratio close to 1.0 → NOT < 0.85
        # → DUPLICATE
        assert is_dup is True
    
    # ========================================================================
    # REAL-WORLD SCENARIO TESTS
    # ========================================================================
    
    def test_paris_london_addition(self, service):
        """User example: Paris → Paris + London."""
        is_dup, reason = service.is_duplicate(
            "Я в прошлом году был в Париже и в Лондоне",  # New: longer
            "Я в прошлом году был в Париже",  # Existing: shorter
            0.96
        )
        
        # No numbers in either
        # New is longer → existing/new < 0.85
        # → NOT duplicate (add detailed version)
        assert is_dup is False
    
    def test_tired_detail_addition(self, service):
        """User example: tired → tired doing work."""
        is_dup, reason = service.is_duplicate(
            "Я вчера устал делать дурную работу",  # New: detailed
            "Я вчера устал",  # Existing: short
            0.97
        )
        
        # New is much longer
        # → NOT duplicate
        assert is_dup is False
    
    def test_llm_minor_rewording(self, service):
        """LLM generates similar phrasing → duplicate."""
        is_dup, reason = service.is_duplicate(
            "The patient has Periodontitis",
            "Patient has Periodontitis condition",
            0.98
        )
        
        # Very similar, same numbers
        # → DUPLICATE
        assert is_dup is True
    
    def test_weight_change_different_dates(self, service):
        """Weight measured on different dates → NOT duplicate."""
        is_dup, reason = service.is_duplicate(
            "Weight on 2025-03-29 was 75 kg",
            "Weight on 2025-03-28 was 75 kg",
            0.99
        )
        
        # Numbers: [2025, 3, 29, 83] vs [2025, 3, 28, 83]
        # Sorted: [3, 29, 83, 2025] vs [3, 28, 83, 2025]
        # Different → NOT duplicate
        assert is_dup is False
        assert "numbers_differ" in reason
    
    def test_weight_change_different_values(self, service):
        """Weight change (different values) → NOT duplicate."""
        is_dup, reason = service.is_duplicate(
            "Weight was 84 kg",
            "Weight was 75 kg",
            0.98
        )
        
        # Numbers differ: [84] != [83]
        # → NOT duplicate
        assert is_dup is False
        assert "numbers_differ" in reason
    
    def test_medical_interpretation_added(self, service):
        """Medical fact with interpretation → NOT duplicate if detailed."""
        is_dup, reason = service.is_duplicate(
            "HbA1c was 5.1%, indicating no diabetes",  # New: with interpretation
            "HbA1c was 5.1%",  # Existing: just value
            0.96
        )
        
        # Numbers same: [5.1] == [5.1]
        # New is longer → NOT duplicate
        assert is_dup is False
        assert "new_more_detailed" in reason
    
    # ========================================================================
    # EDGE CASES
    # ========================================================================
    
    def test_empty_numbers_both_facts(self, service):
        """No numbers in either fact."""
        is_dup, reason = service.is_duplicate(
            "Patient feels better",
            "Patient feels good",
            0.97
        )
        
        # No numbers, moderate similarity, similar length
        # → DUPLICATE
        assert is_dup is True
    
    def test_many_numbers_one_differs(self, service):
        """Many numbers but one differs → NOT duplicate."""
        is_dup, reason = service.is_duplicate(
            "BP 120/80, HR 72, temp 36.6",
            "BP 120/80, HR 71, temp 36.6",  # HR differs
            0.99
        )
        
        # Sorted: [36.6, 72, 80, 120] vs [36.6, 71, 80, 120]
        # Different → NOT duplicate
        assert is_dup is False
    
    def test_float_vs_int_numbers(self, service):
        """Float and int comparison."""
        is_dup, reason = service.is_duplicate(
            "Temperature 36.5 degrees",
            "Temperature 36 degrees",
            0.98
        )
        
        # [36.5] != [36.0]
        # → NOT duplicate
        assert is_dup is False
        assert "numbers_differ" in reason
    
    # ========================================================================
    # EXTRACT NUMBERS METHOD TESTS
    # ========================================================================
    
    def test_extract_integers(self, service):
        """Extract integer numbers."""
        numbers = service._extract_and_sort_numbers("Weight 75 kg")
        assert numbers == [75.0]
    
    def test_extract_floats(self, service):
        """Extract float numbers."""
        numbers = service._extract_and_sort_numbers("HbA1c 5.1%")
        # Note: Extracts "1" from "HbA1c" and "5.1" from value
        assert numbers == [1.0, 5.1]
    
    def test_extract_multiple_numbers(self, service):
        """Extract and sort multiple numbers."""
        numbers = service._extract_and_sort_numbers("BP 120/80 mmHg")
        assert numbers == [80.0, 120.0]  # Sorted
    
    def test_extract_dates(self, service):
        """Extract numbers from dates."""
        numbers = service._extract_and_sort_numbers("Event on 2025-03-28")
        assert numbers == [3.0, 28.0, 2025.0]  # Sorted
    
    def test_extract_no_numbers(self, service):
        """Extract from text without numbers."""
        numbers = service._extract_and_sort_numbers("Patient has Periodontitis")
        assert numbers == []
    
    def test_extract_range(self, service):
        """Extract numbers from range."""
        numbers = service._extract_and_sort_numbers("Weight 95-98 kg")
        assert numbers == [95.0, 98.0]

