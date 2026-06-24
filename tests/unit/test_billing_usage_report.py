"""Unit tests for scripts/billing/usage_report.py pure query-building.

Guards the two things that can silently drift: the pricing CTE must enumerate
every model from the canonical domain pricing, and the cost expression must keep
all four calculate_cost() terms (input, output, cache-read, cache-write).
"""

import importlib.util
from pathlib import Path

from src.domain.billing import _PRICING_PER_MILLION_TOKENS

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "billing" / "usage_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("usage_report", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


usage_report = _load_module()


class TestBuildPricingCte:

    def test_enumerates_every_canonical_model(self):
        cte = usage_report.build_pricing_cte(_PRICING_PER_MILLION_TOKENS)
        for model in _PRICING_PER_MILLION_TOKENS:
            assert f'"{model}"' in cte, f"{model} missing from pricing CTE"

    def test_emits_rates_for_a_known_claude_model(self):
        cte = usage_report.build_pricing_cte(
            {"claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.1, "cache_write": 1.25}}
        )
        assert "3.0 AS inp" in cte
        assert "15.0 AS outp" in cte
        assert "0.1 AS cr" in cte
        assert "1.25 AS cw" in cte

    def test_defaults_missing_cache_multipliers_to_zero(self):
        # grok models carry no cache_read/cache_write keys.
        cte = usage_report.build_pricing_cte({"grok-x": {"input": 0.2, "output": 0.5}})
        assert "0 AS cr" in cte
        assert "0 AS cw" in cte

    def test_starts_as_valid_cte(self):
        cte = usage_report.build_pricing_cte(_PRICING_PER_MILLION_TOKENS)
        assert cte.startswith("WITH pricing AS (")
        assert "UNNEST([" in cte


class TestCostExpression:

    def test_has_all_four_calculate_cost_terms(self):
        expr = usage_report._COST_EXPR
        assert "d.prompt_tokens/1e6*p.inp" in expr            # uncached input
        assert "d.completion_tokens/1e6*p.outp" in expr       # output
        assert "d.cache_read_tokens/1e6*p.inp*p.cr" in expr   # cache read (×input)
        assert "d.cache_creation_tokens/1e6*p.inp*p.cw" in expr  # cache write (×input)
