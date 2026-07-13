from src.services.cost_calculator import calculate_cost


def test_calculate_cost_unknown_model():
    cost = calculate_cost("unknown-model", prompt_tokens=1000, completion_tokens=1000)
    assert cost == 0.0


def test_calculate_cost_gpt56_input_output():
    # GPT-5.6 list prices per 1M: Luna $1/$6, Terra $2.50/$15, Sol $5/$30
    assert calculate_cost("gpt-5.6-luna", prompt_tokens=1_000_000, completion_tokens=0) == 1.00
    assert calculate_cost("gpt-5.6-luna", prompt_tokens=0, completion_tokens=1_000_000) == 6.00
    assert calculate_cost("gpt-5.6-terra", prompt_tokens=1_000_000, completion_tokens=0) == 2.50
    assert calculate_cost("gpt-5.6-terra", prompt_tokens=0, completion_tokens=1_000_000) == 15.00
    assert calculate_cost("gpt-5.6-sol", prompt_tokens=1_000_000, completion_tokens=0) == 5.00
    assert calculate_cost("gpt-5.6-sol", prompt_tokens=0, completion_tokens=1_000_000) == 30.00


def test_calculate_cost_gpt56_cache_read_and_write():
    # cache_read = 0.10x input; cache_write = 1.25x input (new billed dimension on 5.6).
    # Luna input $1/1M → 1M cache-read = $0.10, 1M cache-write = $1.25.
    assert calculate_cost("gpt-5.6-luna", prompt_tokens=0, completion_tokens=0,
                          cache_read_tokens=1_000_000) == 0.10
    assert calculate_cost("gpt-5.6-luna", prompt_tokens=0, completion_tokens=0,
                          cache_creation_tokens=1_000_000) == 1.25
