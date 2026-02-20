from src.services.cost_calculator import calculate_cost


def test_calculate_cost_flash():
    cost = calculate_cost("gemini-3-flash-preview", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost == 0.375


def test_calculate_cost_unknown_model():
    cost = calculate_cost("unknown-model", prompt_tokens=1000, completion_tokens=1000)
    assert cost == 0.0
