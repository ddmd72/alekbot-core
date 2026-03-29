"""
Unit tests for PromptBuilder and UserPromptBuilder.

Coverage targets (missing lines from 57%):
  preload_components()
    - caches system facts into _component_cache
  build_system_prompt()
    - full mode, user_id=None → bio_context=""
    - light mode → returns biographical_context + kernel
    - unknown mode → raises ValueError
    - lens provided → adds lens_instructions
  merge_enriched_context_with_biographical()
    - no enriched context → returns cached bio
    - enriched context merged into list
    - both empty → empty list
  build_for_agent()
    - no assembly_service → raises ValueError
    - include_biographical=False → empty bio
    - account_id repo exception → warning + empty bio
    - user_id only (no account_id) → warning + empty bio
    - qs_facts (semantic_lens tagged) → query_specific_context built
  _get_biographical_component()
    - exception path → returns fallback string
  _format_biographical_facts()
    - empty list → returns placeholder string
  _build_lens_instructions()
    - returns formatted string with name + weights
  invalidate_cache()
    - specific key → removes that entry
    - None → clears all
  get_cache_stats()
    - returns correct stats dict
  UserPromptBuilder._get_component()
    - custom_id found → returns custom text
    - custom_id not found → falls back to super()
    - no custom_id mapped → falls back to super()
"""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.user import UserBotConfig, PromptPreferences
from src.services.prompt_builder import PromptBuilder, UserPromptBuilder
from src.ports.repository import FactRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_repo():
    repo = MagicMock(spec=FactRepository)
    repo.get_active_facts = AsyncMock(return_value=[])
    repo.get_biographical_context_cached = AsyncMock(return_value=[])
    repo.get_latest_fact_by_lineage = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_assembly():
    svc = MagicMock()
    svc.assemble = AsyncMock(return_value="ASSEMBLED")
    return svc


def _make_builder(mock_repo, assembly=None, cache_ttl=3600):
    return PromptBuilder(repo=mock_repo, assembly_service=assembly, cache_ttl=cache_ttl)


# ---------------------------------------------------------------------------
# preload_components()
# ---------------------------------------------------------------------------

class TestPreloadComponents:

    async def test_caches_system_facts(self, mock_repo):
        fact = MagicMock()
        fact.lineage_id = "kernel"
        fact.text = "kernel content"
        mock_repo.get_active_facts = AsyncMock(return_value=[fact])

        builder = _make_builder(mock_repo)
        await builder.preload_components()

        assert "prompt_component:kernel" in builder._component_cache
        cached_content, _ = builder._component_cache["prompt_component:kernel"]
        assert cached_content == "kernel content"

    async def test_empty_system_facts_leaves_cache_empty(self, mock_repo):
        mock_repo.get_active_facts = AsyncMock(return_value=[])
        builder = _make_builder(mock_repo)
        await builder.preload_components()
        assert builder._component_cache == {}


# ---------------------------------------------------------------------------
# build_system_prompt()
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:

    async def test_full_mode_no_user_id_bio_empty(self, mock_repo):
        builder = _make_builder(mock_repo)
        result = await builder.build_system_prompt(mode="full", user_id=None)
        assert result["biographical_context"] == ""
        assert "kernel" in result
        assert "slack_rules" in result

    async def test_light_mode_returns_bio_and_kernel(self, mock_repo):
        mock_repo.get_biographical_context_cached = AsyncMock(
            return_value=[{"text": "bio fact 1"}]
        )
        builder = _make_builder(mock_repo)
        result = await builder.build_system_prompt(mode="light", user_id="user1")
        assert "biographical_context" in result
        assert "kernel" in result
        assert "slack_rules" in result
        assert "- bio fact 1" in result["biographical_context"]

    async def test_light_mode_no_user_id_bio_empty(self, mock_repo):
        builder = _make_builder(mock_repo)
        result = await builder.build_system_prompt(mode="light", user_id=None)
        assert result["biographical_context"] == ""

    async def test_unknown_mode_raises_value_error(self, mock_repo):
        builder = _make_builder(mock_repo)
        with pytest.raises(ValueError, match="Unknown mode"):
            await builder.build_system_prompt(mode="invalid")

    async def test_lens_adds_lens_instructions(self, mock_repo):
        builder = _make_builder(mock_repo)
        lens = {"name": "health", "weights": {"lambda_vector": 1.5, "lambda_recency": 0.8}}
        result = await builder.build_system_prompt(mode="full", lens=lens)
        assert "lens_instructions" in result
        assert "health" in result["lens_instructions"]


# ---------------------------------------------------------------------------
# merge_enriched_context_with_biographical()
# ---------------------------------------------------------------------------

class TestMergeEnrichedContext:

    def test_no_enriched_context_returns_bio(self, mock_repo):
        builder = _make_builder(mock_repo)
        bio = [{"text": "bio fact"}]
        result = builder.merge_enriched_context_with_biographical(None, bio)
        assert result == bio

    def test_empty_enriched_context_returns_bio(self, mock_repo):
        builder = _make_builder(mock_repo)
        bio = [{"text": "bio fact"}]
        result = builder.merge_enriched_context_with_biographical({"facts": []}, bio)
        assert result == bio

    def test_enriched_facts_merged_into_list(self, mock_repo):
        builder = _make_builder(mock_repo)
        enriched = {"facts": [{"content": "enriched fact", "source": "keyword_tags"}]}
        result = builder.merge_enriched_context_with_biographical(enriched, [])
        assert len(result) == 1
        assert result[0]["text"] == "enriched fact"
        assert result[0]["source"] == "keyword_tags"
        assert "semantic_lens" in result[0]["tags"]

    def test_both_empty_returns_empty_list(self, mock_repo):
        builder = _make_builder(mock_repo)
        result = builder.merge_enriched_context_with_biographical(None, None)
        assert result == []

    def test_bio_and_enriched_combined(self, mock_repo):
        builder = _make_builder(mock_repo)
        bio = [{"text": "bio"}]
        enriched = {"facts": [{"content": "semantic", "source": "kw"}]}
        result = builder.merge_enriched_context_with_biographical(enriched, bio)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# build_for_agent()
# ---------------------------------------------------------------------------

class TestBuildForAgent:

    async def test_no_assembly_service_raises(self, mock_repo):
        builder = _make_builder(mock_repo, assembly=None)
        with pytest.raises(ValueError, match="assembly_service is required"):
            await builder.build_for_agent(agent_type="quick")

    async def test_include_biographical_false_skips_repo(self, mock_repo, mock_assembly):
        builder = _make_builder(mock_repo, mock_assembly)
        await builder.build_for_agent(
            agent_type="quick",
            account_id="acct1",
            include_biographical=False,
        )
        mock_repo.get_biographical_context_cached.assert_not_called()
        kwargs = mock_assembly.assemble.call_args.kwargs
        assert kwargs["biographical_facts"] == []

    async def test_account_id_repo_exception_logs_warning(self, mock_repo, mock_assembly):
        mock_repo.get_biographical_context_cached = AsyncMock(side_effect=Exception("db down"))
        builder = _make_builder(mock_repo, mock_assembly)
        # Should not raise
        await builder.build_for_agent(agent_type="quick", account_id="acct1")
        kwargs = mock_assembly.assemble.call_args.kwargs
        assert kwargs["biographical_facts"] == []

    async def test_user_id_only_no_account_id_logs_warning(self, mock_repo, mock_assembly):
        """user_id without account_id → warning + empty bio (strict multi-tenant separation)."""
        builder = _make_builder(mock_repo, mock_assembly)
        await builder.build_for_agent(agent_type="quick", user_id="user1")
        mock_repo.get_biographical_context_cached.assert_not_called()
        kwargs = mock_assembly.assemble.call_args.kwargs
        assert kwargs["biographical_facts"] == []

    async def test_qs_facts_build_query_specific_context(self, mock_repo, mock_assembly):
        """Biographical facts tagged with 'semantic_lens' become query_specific_context."""
        bio = [
            {"text": "normal bio", "tags": []},
            {"text": "semantic fact", "tags": ["semantic_lens"]},
        ]
        builder = _make_builder(mock_repo, mock_assembly)
        await builder.build_for_agent(
            agent_type="smart",
            biographical_facts=bio,
        )
        kwargs = mock_assembly.assemble.call_args.kwargs
        assert kwargs["biographical_facts"] == [{"text": "normal bio", "tags": []}]
        assert kwargs["query_specific_context"] is not None
        assert "semantic fact" in kwargs["query_specific_context"]

    async def test_no_qs_facts_query_specific_context_is_none(self, mock_repo, mock_assembly):
        bio = [{"text": "normal bio", "tags": []}]
        builder = _make_builder(mock_repo, mock_assembly)
        await builder.build_for_agent(agent_type="quick", biographical_facts=bio)
        kwargs = mock_assembly.assemble.call_args.kwargs
        assert kwargs["query_specific_context"] is None


# ---------------------------------------------------------------------------
# _get_biographical_component()
# ---------------------------------------------------------------------------

class TestGetBiographicalComponent:

    async def test_exception_returns_fallback(self, mock_repo):
        mock_repo.get_biographical_context_cached = AsyncMock(side_effect=Exception("fail"))
        builder = _make_builder(mock_repo)
        result = await builder._get_biographical_component("user1")
        assert result == "// Biographical context unavailable"


# ---------------------------------------------------------------------------
# _format_biographical_facts()
# ---------------------------------------------------------------------------

class TestFormatBiographicalFacts:

    def test_empty_list_returns_placeholder(self, mock_repo):
        builder = _make_builder(mock_repo)
        result = builder._format_biographical_facts([])
        assert result == "// No biographical data available yet."

    def test_facts_formatted_as_bullets(self, mock_repo):
        builder = _make_builder(mock_repo)
        facts = [{"text": "fact one"}, {"text": "fact two"}]
        result = builder._format_biographical_facts(facts)
        assert "- fact one" in result
        assert "- fact two" in result


# ---------------------------------------------------------------------------
# _build_lens_instructions()
# ---------------------------------------------------------------------------

class TestBuildLensInstructions:

    def test_returns_formatted_string(self, mock_repo):
        builder = _make_builder(mock_repo)
        lens = {"name": "finance", "weights": {"lambda_vector": 2.0, "lambda_recency": 0.5}}
        result = builder._build_lens_instructions(lens)
        assert "finance" in result
        assert "2.0" in result
        assert "0.5" in result

    def test_missing_name_uses_unknown(self, mock_repo):
        builder = _make_builder(mock_repo)
        result = builder._build_lens_instructions({})
        assert "Unknown" in result


# ---------------------------------------------------------------------------
# invalidate_cache()
# ---------------------------------------------------------------------------

class TestInvalidateCache:

    def test_specific_key_removes_entry(self, mock_repo):
        builder = _make_builder(mock_repo)
        builder._component_cache["prompt_component:kernel"] = ("content", time.time())
        builder.invalidate_cache("kernel")
        assert "prompt_component:kernel" not in builder._component_cache

    def test_specific_key_not_in_cache_is_noop(self, mock_repo):
        builder = _make_builder(mock_repo)
        builder.invalidate_cache("nonexistent")  # must not raise

    def test_none_clears_all(self, mock_repo):
        builder = _make_builder(mock_repo)
        builder._component_cache["a"] = ("x", 1)
        builder._component_cache["b"] = ("y", 2)
        builder.invalidate_cache(None)
        assert builder._component_cache == {}


# ---------------------------------------------------------------------------
# get_cache_stats()
# ---------------------------------------------------------------------------

class TestGetCacheStats:

    def test_empty_cache(self, mock_repo):
        builder = _make_builder(mock_repo)
        stats = builder.get_cache_stats()
        assert stats["total_entries"] == 0
        assert stats["expired_entries"] == 0
        assert stats["cache_ttl_seconds"] == builder.cache_ttl

    def test_fresh_entry_not_expired(self, mock_repo):
        builder = _make_builder(mock_repo)
        builder._component_cache["prompt_component:k"] = ("v", time.time())
        stats = builder.get_cache_stats()
        assert stats["total_entries"] == 1
        assert stats["expired_entries"] == 0

    def test_old_entry_counted_as_expired(self, mock_repo):
        builder = _make_builder(mock_repo, cache_ttl=10)
        old_ts = time.time() - 9999
        builder._component_cache["prompt_component:k"] = ("v", old_ts)
        stats = builder.get_cache_stats()
        assert stats["expired_entries"] == 1


# ---------------------------------------------------------------------------
# UserPromptBuilder — constructor
# ---------------------------------------------------------------------------

class TestUserPromptBuilderInit:

    def test_constructor_sets_user_and_config(self, mock_repo):
        prefs = PromptPreferences(
            custom_kernel_id=None,
            custom_kernel_light_id=None,
            custom_examples_id=None,
        )
        config = UserBotConfig(prompt_preferences=prefs)
        builder = UserPromptBuilder(repo=mock_repo, user_id="user1", config=config)
        assert builder.user_id == "user1"
        assert builder.config is config

    # NOTE: UserPromptBuilder._get_component() is dead code — it calls
    # super()._get_component() which doesn't exist in PromptBuilder.
    # No tests written for it; tracked as tech debt for removal in next refactor.
