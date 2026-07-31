from src.services.cost_calculator import calculate_cost


def test_calculate_cost_unknown_model():
    cost = calculate_cost("unknown-model", prompt_tokens=1000, completion_tokens=1000)
    assert cost == 0.0


def test_calculate_cost_gpt56_input_output():
    # GPT-5.6 list prices per 1M after the 2026-07-30 cut: Luna $0.20/$1.20 (was $1/$6),
    # Terra $2/$12 (was $2.50/$15), Sol $5/$30 (unchanged).
    assert calculate_cost("gpt-5.6-luna", prompt_tokens=1_000_000, completion_tokens=0) == 0.20
    assert calculate_cost("gpt-5.6-luna", prompt_tokens=0, completion_tokens=1_000_000) == 1.20
    assert calculate_cost("gpt-5.6-terra", prompt_tokens=1_000_000, completion_tokens=0) == 2.00
    assert calculate_cost("gpt-5.6-terra", prompt_tokens=0, completion_tokens=1_000_000) == 12.00
    assert calculate_cost("gpt-5.6-sol", prompt_tokens=1_000_000, completion_tokens=0) == 5.00
    assert calculate_cost("gpt-5.6-sol", prompt_tokens=0, completion_tokens=1_000_000) == 30.00


def test_calculate_cost_gpt56_cache_read_and_write():
    # cache_read = 0.10x input; cache_write = 1.25x input (new billed dimension on 5.6).
    # The multipliers survived the 2026-07-30 cut — only the base moved.
    # Luna input $0.20/1M → 1M cache-read = $0.02, 1M cache-write = $0.25.
    assert calculate_cost("gpt-5.6-luna", prompt_tokens=0, completion_tokens=0,
                          cache_read_tokens=1_000_000) == 0.02
    assert calculate_cost("gpt-5.6-luna", prompt_tokens=0, completion_tokens=0,
                          cache_creation_tokens=1_000_000) == 0.25
