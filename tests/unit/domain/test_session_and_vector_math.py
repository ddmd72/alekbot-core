"""
Unit tests for Session domain model and vector_math utilities.

Pure domain logic — no mocks, no external dependencies.
"""

import math
import pytest

from src.domain.session import Session, SessionState
from src.domain.vector_math import cosine_similarity


# =============================================================================
# Session
# =============================================================================

class TestSession:

    def test_default_creation(self):
        s = Session(session_id="s1", user_id="u1")
        assert s.session_id == "s1"
        assert s.user_id == "u1"
        assert s.history == []
        assert s.message_count == 0

    def test_messages_property_returns_history(self):
        s = Session()
        s.history = ["msg1", "msg2"]
        assert s.messages == ["msg1", "msg2"]

    def test_messages_setter_updates_count(self):
        s = Session()
        s.messages = ["a", "b", "c"]
        assert s.message_count == 3

    def test_owner_id_alias_for_user_id(self):
        s = Session(user_id="owner-123")
        assert s.owner_id == "owner-123"

    def test_owner_id_setter(self):
        s = Session()
        s.owner_id = "new-owner"
        assert s.user_id == "new-owner"

    def test_add_message_appends_and_counts(self):
        s = Session()
        s.add_message("hello")
        s.add_message("world")
        assert len(s.history) == 2
        assert s.message_count == 2

    def test_add_message_updates_timestamps(self):
        s = Session()
        before = s.updated_at
        import time; time.sleep(0.01)
        s.add_message("hi")
        assert s.updated_at >= before
        assert s.last_activity == s.updated_at

    def test_should_consolidate_above_threshold(self):
        s = Session()
        s.history = list(range(101))
        assert s.should_consolidate(threshold=100) is True

    def test_should_consolidate_at_or_below_threshold(self):
        s = Session()
        s.history = list(range(100))
        assert s.should_consolidate(threshold=100) is False

    def test_extract_oldest_messages_removes_from_front(self):
        s = Session()
        s.history = [1, 2, 3, 4, 5]
        batch = s.extract_oldest_messages(3)
        assert batch == [1, 2, 3]
        assert s.history == [4, 5]
        assert s.message_count == 2

    def test_extract_oldest_sets_consolidation_timestamp(self):
        s = Session()
        s.history = ["a", "b"]
        assert s.last_consolidation_at is None
        s.extract_oldest_messages(1)
        assert s.last_consolidation_at is not None

    def test_session_state_alias(self):
        assert SessionState is Session


# =============================================================================
# cosine_similarity
# =============================================================================

class TestCosineSimilarity:

    def test_identical_vectors_return_one(self):
        v = [1.0, 0.5, 0.3]
        result = cosine_similarity(v, v)
        assert abs(result - 1.0) < 1e-9

    def test_orthogonal_vectors_return_zero(self):
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        result = cosine_similarity(v1, v2)
        assert abs(result) < 1e-9

    def test_similar_vectors(self):
        v1 = [1.0, 1.0, 0.0]
        v2 = [1.0, 1.0, 0.0]
        assert abs(cosine_similarity(v1, v2) - 1.0) < 1e-9

    def test_zero_vector_returns_zero(self):
        v1 = [0.0, 0.0, 0.0]
        v2 = [1.0, 2.0, 3.0]
        result = cosine_similarity(v1, v2)
        assert result == 0.0

    def test_both_zero_returns_zero(self):
        v1 = [0.0, 0.0]
        v2 = [0.0, 0.0]
        assert cosine_similarity(v1, v2) == 0.0

    def test_range_is_zero_to_one_for_non_negative_vectors(self):
        v1 = [0.3, 0.7, 0.2]
        v2 = [0.1, 0.9, 0.4]
        result = cosine_similarity(v1, v2)
        assert 0.0 <= result <= 1.0

    def test_high_dimensional_vectors(self):
        dim = 768
        v = [1.0 / math.sqrt(dim)] * dim
        result = cosine_similarity(v, v)
        assert abs(result - 1.0) < 1e-6
