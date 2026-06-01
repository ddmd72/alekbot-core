"""Unit tests for ProfileToken — token assignment entry (Prompt System v4).

RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md
Covers from_dict deserialization and to_dict compact serialization.
"""

from src.domain.prompt_v3.profile_slot import ProfileToken


class TestProfileTokenFromDict:
    def test_minimal_entry_defaults_non_overridable_false(self):
        t = ProfileToken.from_dict("HUMOR_PRESET_RANEVSKAYA", {"order": 40})
        assert t.token_id == "HUMOR_PRESET_RANEVSKAYA"
        assert t.order == 40
        assert t.non_overridable is False

    def test_locked_entry(self):
        t = ProfileToken.from_dict(
            "COGNITIVE_PROCESS_QUICK", {"order": 10, "non_overridable": True}
        )
        assert t.non_overridable is True

    def test_order_coerced_to_int(self):
        t = ProfileToken.from_dict("X", {"order": "30"})
        assert t.order == 30


class TestProfileTokenToDict:
    def test_minimal_omits_non_overridable(self):
        t = ProfileToken(token_id="X", order=20)
        assert t.to_dict() == {"order": 20}

    def test_includes_non_overridable_when_true(self):
        t = ProfileToken(token_id="X", order=20, non_overridable=True)
        assert t.to_dict() == {"order": 20, "non_overridable": True}

    def test_round_trip(self):
        original = {"order": 10, "non_overridable": True}
        assert ProfileToken.from_dict("X", original).to_dict() == original
