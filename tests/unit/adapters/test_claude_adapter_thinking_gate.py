"""ClaudeAdapter.takes_adaptive_thinking is the one gate for adaptive thinking + effort."""
import pytest

from src.adapters.claude_adapter import ClaudeAdapter


@pytest.mark.parametrize("model, expected", [
    ("claude-fable-5-2", True),
    ("claude-sonnet-5-5", True),
    ("claude-haiku-5-5", True),
    ("claude-haiku-4-5-20251001", False),
])
def test_takes_adaptive_thinking_matches_the_gate(model, expected):
    assert ClaudeAdapter.takes_adaptive_thinking(model) is expected
