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


def test_calculate_cost_grok_input_output():
    # xAI list prices per 1M, verified against the live rate card 2026-08-14.
    # grok-4.6 undercuts Sonnet 5 on output ($6 vs $10) at the same $2 input.
    assert calculate_cost("grok-4.6", prompt_tokens=1_000_000, completion_tokens=0) == 2.00
    assert calculate_cost("grok-4.6", prompt_tokens=0, completion_tokens=1_000_000) == 6.00
    assert calculate_cost("grok-4.5", prompt_tokens=1_000_000, completion_tokens=0) == 2.00
    assert calculate_cost("grok-4.5", prompt_tokens=0, completion_tokens=1_000_000) == 6.00
    assert calculate_cost("grok-4.3", prompt_tokens=1_000_000, completion_tokens=0) == 1.25
    assert calculate_cost("grok-4.3", prompt_tokens=0, completion_tokens=1_000_000) == 2.50


def test_calculate_cost_grok_cache_read():
    # xAI caches automatically; cache_read is the cached-input price as a multiplier
    # of input — 4.6: 0.50/2.00, 4.5: 0.30/2.00, 4.3: 0.20/1.25.
    assert calculate_cost("grok-4.6", prompt_tokens=0, completion_tokens=0,
                          cache_read_tokens=1_000_000) == 0.50
    assert calculate_cost("grok-4.5", prompt_tokens=0, completion_tokens=0,
                          cache_read_tokens=1_000_000) == 0.30
    assert round(calculate_cost("grok-4.3", prompt_tokens=0, completion_tokens=0,
                                cache_read_tokens=1_000_000), 4) == 0.20


def test_calculate_cost_grok_charges_nothing_for_cache_writes():
    """Unlike GPT-5.6 (1.25x input), xAI does not bill cache creation."""
    assert calculate_cost("grok-4.6", prompt_tokens=0, completion_tokens=0,
                          cache_creation_tokens=1_000_000) == 0.0


def test_retired_grok_ids_priced_far_below_what_actually_runs():
    """grok-4-1-fast-* are retired: absent from GET /v1/models, yet they still answer
    HTTP 200 while xAI serves grok-4.3. Requesting one does not fail, it MIS-BILLS —
    which is why MODEL_TIERS must only carry IDs the models endpoint lists. These
    entries are kept solely so historical rows still price."""
    retired = calculate_cost("grok-4-1-fast-non-reasoning",
                             prompt_tokens=1_000_000, completion_tokens=1_000_000)
    actually_served = calculate_cost("grok-4.3",
                                     prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert retired == 0.70
    assert actually_served == 3.75
    assert actually_served > retired * 5
