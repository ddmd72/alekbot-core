# OpenAI Personality Grounding via Developer Role

**Date:** 2026-08-02  
**Status:** Implemented  
**Affects:** SmartResponseAgent on OpenAI (gpt-5.6-terra, gpt-5.6-luna)

## Problem

SmartResponseAgent on OpenAI was losing personality (humor, voice, sardonic tone) despite having full personality instructions in the 730-line system prompt. Personality instructions buried in verbose scaffolding were being ignored by the model.

## Root Cause

OpenAI 2026 models prioritize clarity over scaffolding. Personality instructions in a 730-line system prompt don't receive sufficient attention weight compared to the immediate user message context.

## Solution

**Three-layer approach:**

1. **Increase token budget** (SmartAgentConfig.max_tokens → 64,000)
   - Real prompts consume ~10k tokens before answer space
   - Prevents truncation mid-response

2. **Redesign USER_TURN_SYSTEM_ANCHOR** (base_agent.py)
   - Replaced vague "Manipulate the user" with concrete NLP techniques
   - Specifies: Reframing, Presupposition, Metaphor, Pattern interrupt, Calibration

3. **Extract anchor to developer_message for OpenAI** (openai_adapter.py)
   - Extract USER_TURN_SYSTEM_ANCHOR from last user message
   - Inject into developer_message role (higher attention weight)
   - Combine with personality_anchor (relative references to voice, humor_engine, identity blocks)
   - Result: high-priority override layer for OpenAI

**For Claude/Gemini:** No changes. Their system_instruction approach already provides high priority.

## Trade-offs

| Aspect | Choice | Why |
|--------|--------|-----|
| Hexagonal purity | Adapter-only logic | OpenAI-specific details never leak to agent layer |
| Detection method | "humor_engine" marker | Only user-customized prompts have personality blocks |
| Token budget | 64,000 | Model max; prevents truncation on real prompts |

## Implementation Details

- **Files changed:**
  - `src/infrastructure/agent_config.py` — max_tokens: 64,000
  - `src/agents/base_agent.py` — USER_TURN_SYSTEM_ANCHOR with NLP techniques
  - `src/adapters/openai_adapter.py` — anchor extraction & developer_message injection
  - `src/agents/core/smart_response_agent.py` — pass max_tokens to LLMRequest

- **Test coverage:** 130 tests pass (124 existing + 6 new)

## Validation

Tested on production requests:
- Filmoteca d'Estiu question: ✅ Personality present (paradox, dark humor, self-deprecation)
- "Piwko doma" redirect: ✅ Presupposition + Reframing + Metaphor all working
- Bot successfully guides user to conclusion without direct argument

## Decision

Approved for production. Personality grounding works as intended: bot has opinions, uses NLP techniques to guide, respects user autonomy.
