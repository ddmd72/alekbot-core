"""Provider, tier and limits of the Search v2 enrichment agents (USER_DRIVE_INDEXING_RFC §25.4.4, D7 rev 3.17): the
profiler and the passages agent resolve like the reader without vision — OpenAI gpt-6-luna at ECO by default, Claude
by a user's switch, Gemini Flash (BALANCED) as the fallback only. Their output caps and timeouts are §25.12's, and the
caps hold the schemas' worst case plus a reasoning allowance."""
import json
from unittest.mock import MagicMock

import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.drive_index_params import (
    ENTITY_FORMS_MAX, HINT_EVIDENCE_WORDS_MAX, HINT_KEYWORDS_MAX, HINT_QUESTIONS_MAX, PASSAGE_CHUNKS_PER_CALL,
    PASSAGES_OUTPUT_TOKENS, PASSAGES_TIMEOUT_S, PHOTO_DESCRIPTION_CHARS, PHOTO_OBJECT_FORMS_MAX, PHOTO_OBJECTS_MAX,
    PHOTO_SCENE_WORDS, PHOTO_VISIBLE_TEXT_CHARS, PROFILE_DATES_MAX, PROFILE_ENTITIES_MAX, PROFILE_KEY_VALUES_MAX,
    PROFILE_TOPICS_MAX, PROFILE_WORD_LIST_MAX, PROFILER_OUTPUT_TOKENS, PROFILER_TIMEOUT_S, TRIAGE_BATCH,
    TRIAGE_OUTPUT_TOKENS, TRIAGE_TIMEOUT_S,
)
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.agent_config import DRIVE_PASSAGES, DRIVE_PROFILER, DRIVE_TRIAGE
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry

AGENTS = ["drive_profiler", "drive_passages"]


def _builder(monkeypatch):
    monkeypatch.delenv("CLAUDE_SMALL_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TIER_OVERRIDES", raising=False)
    registry = ProviderRegistry()
    registry.register("claude", ClaudeAdapter(api_key="k"))
    registry.register("openai", OpenAIAdapter(api_key="k"))
    registry.register("gemini", GeminiAdapter(api_key="k"))
    resilience = MagicMock()
    resilience.is_provider_open.return_value = False
    return AgentContextBuilder(registry, resilience_port=resilience)


@pytest.mark.parametrize("agent_type", AGENTS)
def test_default_is_openai_gpt_6_luna_at_eco(monkeypatch, agent_type):
    assert UserBotConfig().get_tier_for_agent(agent_type) is PerformanceTier.ECO
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig())
    assert (ctx.provider_name, ctx.tier, ctx.model_name) == ("openai", PerformanceTier.ECO, "gpt-6-luna")


@pytest.mark.parametrize("agent_type", AGENTS)
def test_a_user_switch_to_claude_resolves_haiku(monkeypatch, agent_type):
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig(agent_providers={agent_type: "claude"}))
    assert (ctx.provider_name, ctx.model_name) == ("claude", "claude-haiku-5-5")


@pytest.mark.parametrize("agent_type", AGENTS)
def test_gemini_is_never_a_primary(monkeypatch, agent_type):
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig(provider_preference="gemini"))
    assert ctx.provider_name == "openai"


@pytest.mark.parametrize("agent_type", AGENTS)
def test_gemini_flash_is_the_fallback_at_balanced(monkeypatch, agent_type):
    strategy = AgentProviderStrategy.get_strategy(agent_type)
    assert strategy["allowed_providers"] == ["claude", "openai"] and strategy["required_capabilities"] == []
    assert strategy["fallback"] == "gemini" and strategy["fallback_tier"] is PerformanceTier.BALANCED
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig())
    assert (ctx.fallback_provider_name, ctx.fallback_model_name) == ("gemini", "gemini-3.8-flash")


def test_limits_are_the_rfc_parameters():
    assert (DRIVE_PROFILER.max_tokens, DRIVE_PROFILER.call_timeout_s) == (PROFILER_OUTPUT_TOKENS, PROFILER_TIMEOUT_S)
    assert (DRIVE_PASSAGES.max_tokens, DRIVE_PASSAGES.call_timeout_s) == (PASSAGES_OUTPUT_TOKENS, PASSAGES_TIMEOUT_S)
    assert (DRIVE_TRIAGE.max_tokens, DRIVE_TRIAGE.call_timeout_s) == (TRIAGE_OUTPUT_TOKENS, TRIAGE_TIMEOUT_S)
    assert (PROFILER_OUTPUT_TOKENS, PASSAGES_OUTPUT_TOKENS, TRIAGE_OUTPUT_TOKENS) == (4_096, 4_096, 8_192)
    assert TRIAGE_BATCH == 8
    for config in (DRIVE_PROFILER, DRIVE_PASSAGES, DRIVE_TRIAGE):
        assert config.thinking == "low" and config.max_parse_retries == 1


# -- the caps hold the schema's worst case (§25.4.4, §25.12) ---------------------------------------------------------
# Typical lengths at the list caps: a word or name of 14 characters, a form of 12, a phrase of 30, an evidence quote
# of HINT_EVIDENCE_WORDS_MAX words of 7 characters. Tokens at 3 characters each (mixed Latin and Cyrillic, JSON
# punctuation included); "low" reasoning gets a 1 000-token allowance.
CHARS_PER_TOKEN = 3.0
REASONING_ALLOWANCE = 1_000


def _tokens(payload) -> int:
    return int(len(json.dumps(payload, ensure_ascii=False)) / CHARS_PER_TOKEN)


def test_profiler_worst_case_fits():
    w = "x" * 14
    profile = {
        "title": "t" * 120, "what_it_is": "w" * 300, "language": "es", "doc_type": "insurance_policy",
        "life_domains": ["legal_admin", "finance", "vehicle"],
        "dates": [{"label": "valid_until", "date": "2026-01-01"}] * PROFILE_DATES_MAX,
        "entities": [{"name": w, "type": "organization", "printed": w,
                      "forms": ["f" * 12] * ENTITY_FORMS_MAX}] * PROFILE_ENTITIES_MAX,
        "key_values": [{"label": "booking_reference", "printed": "p" * 20, "canonical": "c" * 20}]
        * PROFILE_KEY_VALUES_MAX,
        "topics": ["p" * 30] * PROFILE_TOPICS_MAX, "topics_native": ["p" * 30] * PROFILE_TOPICS_MAX,
        **{k: [w] * PROFILE_WORD_LIST_MAX for k in ("topic_words", "label_words", "domain_words", "validity_words")},
    }
    assert _tokens(profile) + REASONING_ALLOWANCE <= PROFILER_OUTPUT_TOKENS


def test_passages_worst_case_fits():
    evidence = " ".join(["e" * 7] * HINT_EVIDENCE_WORDS_MAX)
    passage = {"index": 1, "items": [{"type": "keyword", "text": "k" * 14, "evidence": evidence}] * HINT_KEYWORDS_MAX
               + [{"type": "question", "text": "q" * 60, "evidence": evidence}] * HINT_QUESTIONS_MAX}
    assert _tokens({"passages": [passage] * PASSAGE_CHUNKS_PER_CALL}) + REASONING_ALLOWANCE <= PASSAGES_OUTPUT_TOKENS


def test_triage_worst_case_fits():
    image = {"index": 1, "verdict": "photo", "description": "d" * PHOTO_DESCRIPTION_CHARS,
             "objects": [{"name": "o" * 14, "forms": ["f" * 12] * PHOTO_OBJECT_FORMS_MAX}] * PHOTO_OBJECTS_MAX,
             "scene": " ".join(["s" * 7] * PHOTO_SCENE_WORDS), "visible_text": "v" * PHOTO_VISIBLE_TEXT_CHARS}
    assert _tokens({"images": [image] * TRIAGE_BATCH}) + REASONING_ALLOWANCE <= TRIAGE_OUTPUT_TOKENS
