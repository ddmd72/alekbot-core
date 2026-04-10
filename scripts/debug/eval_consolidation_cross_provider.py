#!/usr/bin/env python3
"""
eval_consolidation_cross_provider.py
====================================

Replay a recorded consolidation session through alternative LLM providers
(OpenAI, Gemini) to compare quality vs the original Claude run.

DRY RUN — no Firestore writes. The fact-management tool dispatcher is
monkey-patched to look up cached responses from the original session, falling
back to synthetic empty results for cache misses.

Usage:
    python scripts/debug/eval_consolidation_cross_provider.py \\
        --session 2026-04-10/2026-04-10_10-32-29 \\
        --providers openai,gemini \\
        --output scripts/memory/eval_consolidation_2026-04-10/

Inputs:
    --session   Path prefix of the FIRST request file in the session, relative
                to gs://gen-lang-client-0554950952-debug-prompts/consolidation/.
                Example: 2026-04-10/2026-04-10_10-32-29

How it works:
    1. Lists files for that date dir, picks the run starting at the given
       timestamp, walks forward until the final response (no tool_calls).
    2. Parses each request/response pair: extracts system_prompt + initial
       user message from turn 1; pairs each LLM tool_call with the
       tool_response that came back in the next turn (order-preserved).
    3. Builds {(tool_name, args_json) -> result_str} cache.
    4. Constructs ConsolidationAgent against each target provider with
       a stub repository and patched _execute_fact_management_tools.
    5. Runs _run_consolidation_loop with the original system + initial
       user message. Captures operations report, token counts, durations.
    6. Dumps per-provider JSON + diff summary.
"""

import argparse
import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

# Make repo importable
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.agents.consolidation_agent import ConsolidationAgent, ToolResponse
from src.domain.agent import AgentConfig
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, ToolCall


GCS_BUCKET = "gen-lang-client-0554950952-debug-prompts"
DEBUG_PREFIX = "consolidation"


# ----------------------------------------------------------------------------
# GCS helpers
# ----------------------------------------------------------------------------

def gsutil_cat(gcs_path: str) -> str:
    result = subprocess.run(
        ["gsutil", "cat", gcs_path],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def gsutil_ls(gcs_path: str) -> List[str]:
    result = subprocess.run(
        ["gsutil", "ls", gcs_path],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.strip().split("\n") if line.strip()]


def list_session_files(session_prefix: str) -> List[str]:
    """Walk forward from the starting request file until we hit the final
    response (one with operations / no tool_calls). Returns sorted list of
    request+response files for the session.
    """
    date_dir = session_prefix.split("/")[0]
    all_files = sorted(gsutil_ls(f"gs://{GCS_BUCKET}/{DEBUG_PREFIX}/{date_dir}/"))

    start_marker = f"{DEBUG_PREFIX}/{session_prefix}_request.txt"
    start_idx = None
    for i, f in enumerate(all_files):
        if f.endswith(start_marker):
            start_idx = i
            break
    if start_idx is None:
        raise ValueError(f"Session prefix '{session_prefix}' not found in {date_dir}/")

    # Take everything from start_idx until the next request file that starts
    # a NEW session (heuristic: gap > 60s OR explicit termination via final
    # response detection). Simpler: walk pairs (req, resp) until a response
    # has no tool_calls (= final report).
    selected = []
    i = start_idx
    while i < len(all_files):
        f = all_files[i]
        selected.append(f)
        if "_response.txt" in f:
            content = gsutil_cat(f)
            tc_match = re.search(r"=== TOOL CALLS ===", content)
            if not tc_match:
                # Final response — done.
                break
        i += 1
    return selected


# ----------------------------------------------------------------------------
# File parsing
# ----------------------------------------------------------------------------

def parse_request_file(content: str) -> Dict[str, Any]:
    """Parse a debug request file written by PromptDebugLogger.log_llm_request.

    Returns:
        {model, temperature, turn, system, user_messages: List[str]}

    Note: each [user] / [model] section in the file may contain a Python repr
    of List[MessagePart] when the underlying message has no plain text parts.
    We keep the raw text — extraction of tool_responses is done downstream.
    """
    header_match = re.search(r"MODEL:\s*(\S+)", content)
    model = header_match.group(1) if header_match else ""

    temp_match = re.search(r"temperature:\s*([\d.]+)", content)
    temperature = float(temp_match.group(1)) if temp_match else 1.0

    turn_match = re.search(r"TURN:\s*(\d+)", content)
    turn = int(turn_match.group(1)) if turn_match else 1

    # Skip header (between two `===...` lines)
    sep = "=" * 80
    body_start = content.find(sep)
    if body_start != -1:
        body_start = content.find(sep, body_start + len(sep))
        body_start = content.find("\n", body_start) + 1 if body_start != -1 else 0
    body = content[body_start:]

    role_re = re.compile(r"^\[(system|user|model)\]\s*$", re.MULTILINE)
    matches = list(role_re.finditer(body))
    sections: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        role = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[start:end].strip()
        sections.append((role, text))

    system = next((t for r, t in sections if r == "system"), "")
    user_messages = [t for r, t in sections if r == "user"]

    return {
        "model": model,
        "temperature": temperature,
        "turn": turn,
        "system": system,
        "user_messages": user_messages,
    }


def parse_response_file(content: str) -> Dict[str, Any]:
    """Parse a debug response file.

    Returns:
        {turn, text, tool_calls: List[dict], operations: Optional[List], tokens: int}
    """
    turn_match = re.search(r"METADATA:.*'turn':\s*(\d+)", content)
    turn = int(turn_match.group(1)) if turn_match else 1

    text_match = re.search(r"=== TEXT ===\s*\n(.*?)(?=\n=== |\Z)", content, re.DOTALL)
    text = text_match.group(1).strip() if text_match else ""

    tool_calls: List[Dict] = []
    tc_match = re.search(r"=== TOOL CALLS ===\s*\n(.*?)(?=\n=== |\Z)", content, re.DOTALL)
    if tc_match:
        try:
            tool_calls = json.loads(tc_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    operations: Optional[List[Dict]] = None
    json_match = re.search(r"=== JSON ===\s*\n(.*?)(?=\n=== |\Z)", content, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1).strip())
            operations = data.get("operations", [])
        except json.JSONDecodeError:
            pass

    tokens = 0
    tok_match = re.search(r"=== TOKENS:\s*(\d+)", content)
    if tok_match:
        tokens = int(tok_match.group(1))

    return {
        "turn": turn,
        "text": text,
        "tool_calls": tool_calls,
        "operations": operations,
        "tokens": tokens,
    }


def extract_tool_responses_from_user_section(user_text: str) -> List[Dict]:
    """Extract tool_response dicts from a [user] section that contains a
    Python repr of `[MessagePart(...), MessagePart(...)]`.

    The repr is NOT ast.literal_eval-safe (contains class-call syntax), but
    each `tool_response={...}` substring IS a Python dict literal. We scan
    for it, walk balanced braces respecting string state, then literal_eval.
    """
    results: List[Dict] = []
    needle = "tool_response="
    pos = 0
    while True:
        idx = user_text.find(needle, pos)
        if idx == -1:
            break
        start = idx + len(needle)
        if start >= len(user_text) or user_text[start] != "{":
            pos = start
            continue

        # Balanced brace scan, respecting string boundaries
        depth = 0
        i = start
        in_str = False
        str_char: Optional[str] = None
        end: Optional[int] = None
        while i < len(user_text):
            c = user_text[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == str_char:
                    in_str = False
            else:
                if c in ("'", '"'):
                    in_str = True
                    str_char = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            i += 1

        if end is None:
            break

        try:
            d = ast.literal_eval(user_text[start:end])
            results.append(d)
        except (ValueError, SyntaxError) as e:
            print(f"⚠️  Failed to parse tool_response literal: {e}")

        pos = end
    return results


# ----------------------------------------------------------------------------
# Replay cache construction
# ----------------------------------------------------------------------------

def build_replay_cache(
    session_files: List[str],
) -> Tuple[Dict[Tuple[str, str], str], Dict[str, Any]]:
    """Read session files, build {(tool_name, args_json) -> result_str} cache.

    Returns:
        (cache, baseline) where baseline contains:
            {system, initial_user_message, claude_model, claude_operations,
             claude_tokens, claude_turns}
    """
    requests: List[Dict] = []
    responses: List[Dict] = []
    for f in session_files:
        content = gsutil_cat(f)
        if "_request.txt" in f:
            requests.append(parse_request_file(content))
        elif "_response.txt" in f:
            responses.append(parse_response_file(content))

    requests.sort(key=lambda r: r["turn"])
    responses.sort(key=lambda r: r["turn"])

    if not requests:
        raise RuntimeError("No request files found")

    system = requests[0]["system"]
    initial_user_msg = (
        requests[0]["user_messages"][0]
        if requests[0]["user_messages"]
        else "Begin deliberate consolidation."
    )

    # Pair turn N response tool_calls with turn N+1 request tool_responses
    cache: Dict[Tuple[str, str], str] = {}
    for i, resp in enumerate(responses):
        if not resp["tool_calls"]:
            continue
        if i + 1 >= len(requests):
            print(f"⚠️  Turn {resp['turn']} has tool_calls but no subsequent request")
            break
        next_req = requests[i + 1]
        if not next_req["user_messages"]:
            continue
        last_user_text = next_req["user_messages"][-1]
        tool_responses = extract_tool_responses_from_user_section(last_user_text)

        if len(tool_responses) != len(resp["tool_calls"]):
            print(
                f"⚠️  Turn {resp['turn']}: extracted "
                f"{len(tool_responses)} tool_responses for "
                f"{len(resp['tool_calls'])} tool_calls"
            )

        for tc, tr in zip(resp["tool_calls"], tool_responses):
            args = tc.get("args", {})
            key = (tc["name"], json.dumps(args, sort_keys=True, ensure_ascii=False))
            result_str = tr.get("response", {}).get("result", "")
            cache[key] = result_str

    # Find Claude baseline operations from the final response
    claude_operations: Optional[List[Dict]] = None
    claude_tokens = 0
    for resp in reversed(responses):
        if resp["operations"] is not None:
            claude_operations = resp["operations"]
            claude_tokens = resp["tokens"]
            break

    return cache, {
        "system": system,
        "initial_user_message": initial_user_msg,
        "claude_model": requests[0]["model"],
        "claude_operations": claude_operations or [],
        "claude_tokens": claude_tokens,
        "claude_turns": len(requests),
    }


# ----------------------------------------------------------------------------
# Replay execution
# ----------------------------------------------------------------------------

def build_unified_fact_pool(cache: Dict[Tuple[str, str], str]) -> str:
    """Aggregate all unique facts from cached search_existing_facts results.

    Returns a JSON-serialized list of unique fact dicts (deduped by fact_id).
    Used to answer ANY search_existing_facts call from a replayed provider —
    we don't care that their query keywords differ from Claude's, we just want
    them to see the full universe of facts Claude could see in the session.
    """
    seen_ids: set = set()
    pool: List[Dict] = []
    for (name, _args), result_str in cache.items():
        if name != "search_existing_facts":
            continue
        try:
            facts = json.loads(result_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fid = fact.get("fact_id")
            if fid and fid not in seen_ids:
                seen_ids.add(fid)
                pool.append(fact)
    return json.dumps(pool, ensure_ascii=False)


def synthetic_fallback(tc: ToolCall) -> str:
    """Plausible empty/ok response for cache misses."""
    if tc.name == "search_existing_facts":
        return "[]"
    if tc.name == "count_words":
        text = tc.args.get("text", "") if isinstance(tc.args, dict) else ""
        wc = len(text.split())
        return json.dumps({
            "word_count": wc,
            "limit": 40,
            "within_limit": wc <= 40,
            "excess": max(0, wc - 40),
        })
    if tc.name in ("create_fact", "merge_facts"):
        return json.dumps({
            "fact_id": f"stub_{uuid4().hex[:8]}",
            "status": "ok",
        })
    if tc.name == "update_fact":
        fact_id = tc.args.get("fact_id", "") if isinstance(tc.args, dict) else ""
        return json.dumps({"fact_id": fact_id, "status": "ok", "version": 99})
    return json.dumps({"status": "ok"})


class StubRepo:
    """Minimal repo stub. Only get_biographical_context_cached is invoked
    inside _run_consolidation_loop (CPU keepalive)."""

    async def get_biographical_context_cached(self, **kwargs) -> List[Dict]:
        return []


def _patch_gemini_for_flex(adapter, request_timeout_s: int) -> None:
    """Wrap adapter's underlying genai client to enable Flex Inference.

    Modifications applied to every generate_content call:
      - service_tier='flex' on the GenerateContentConfig
      - thinking_config.thinking_budget=-1 (dynamic budget)
      - HTTP timeout bumped to request_timeout_s seconds (Flex queues 1-15 min)
      - 2 retries with 60s backoff on 503/429 (no automatic standard fallback)
    """
    from google.genai import types as gtypes
    from google.genai import errors as gerrors

    original_generate = adapter.client.aio.models.generate_content

    async def flex_generate(*, model, contents, config=None):
        # Mutate the config: inject service_tier and thinking_budget
        if config is None:
            config = gtypes.GenerateContentConfig()
        # GenerateContentConfig is a pydantic model — mutate via copy.update
        try:
            patched = config.model_copy(update={"service_tier": "flex"})
        except AttributeError:
            # fallback for non-pydantic
            config.service_tier = "flex"
            patched = config

        # ThinkingConfig: leave whatever the adapter set untouched.
        # On Gemini 3.x, thinking_level=HIGH IS dynamic — there is no separate
        # "budget=-1" mode for these models. Google docs (Vertex AI Thinking):
        #     "By default, Gemini 3 models use dynamic thinking
        #      (thinking_level.HIGH) to reason through prompts."
        # And: "thinking_budget is accepted for backwards compatibility but
        # using it with Gemini 3 Pro may result in suboptimal performance."
        # Our adapter already maps request.thinking="high" → thinking_level=HIGH,
        # which is the recommended path. We simply do NOT overwrite it here.

        # Bump per-call HTTP timeout
        if patched.http_options is None:
            patched = patched.model_copy(
                update={
                    "http_options": gtypes.HttpOptions(
                        timeout=request_timeout_s * 1000
                    )
                }
            )
        else:
            patched_http = patched.http_options.model_copy(
                update={"timeout": request_timeout_s * 1000}
            )
            patched = patched.model_copy(update={"http_options": patched_http})

        last_exc: Optional[Exception] = None
        for attempt in range(3):  # 1 try + 2 retries
            try:
                return await original_generate(
                    model=model, contents=contents, config=patched
                )
            except gerrors.ClientError as e:
                code = getattr(e, "code", None)
                if code != 429:
                    raise
                last_exc = e
                if attempt < 2:
                    print(
                        f"   ⏳ flex 429 on attempt {attempt + 1}/3 — "
                        f"backing off 60s..."
                    )
                    await asyncio.sleep(60)
            except gerrors.ServerError as e:
                code = getattr(e, "code", None)
                if code != 503:
                    raise
                last_exc = e
                if attempt < 2:
                    print(
                        f"   ⏳ flex 503 on attempt {attempt + 1}/3 — "
                        f"backing off 60s..."
                    )
                    await asyncio.sleep(60)
        # Out of retries
        assert last_exc is not None
        raise last_exc

    adapter.client.aio.models.generate_content = flex_generate  # type: ignore


async def replay_through_provider(
    provider_name: str,
    system_prompt: str,
    initial_user_message: str,
    cache: Dict[Tuple[str, str], str],
    unified_fact_pool: str,
    model_override: Optional[str] = None,
    thinking_override: Optional[str] = None,
    flex: bool = False,
    max_tokens_override: Optional[int] = None,
    temperature_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Spin up a ConsolidationAgent against the given provider, run the loop."""
    if provider_name == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        adapter = OpenAIAdapter(api_key=api_key)
    elif provider_name == "gemini":
        api_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
        )
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY or GEMINI_API_KEY not set")
        adapter = GeminiAdapter(api_key=api_key)
    else:
        raise ValueError(f"Unsupported provider: {provider_name}")

    if flex and provider_name != "gemini":
        raise ValueError("--flex is only supported for gemini provider")

    model = model_override or adapter.MODEL_TIERS[PerformanceTier.PERFORMANCE]

    # Flex tier can queue 1-15 min per call. ConsolidationAgent loop allows
    # up to 15 turns → worst case 15×15min = 225 min total. Bump the agent
    # config timeout accordingly so we don't bail before flex completes.
    agent_timeout_ms = 14_400_000 if flex else 900_000  # 4h for flex, 15min std

    if flex:
        request_timeout_s = 1800  # 30 min per LLM call
        _patch_gemini_for_flex(adapter, request_timeout_s=request_timeout_s)
        print("   🪐 flex inference enabled (service_tier='flex', "
              f"thinking_budget=-1, http_timeout={request_timeout_s}s, "
              "retries=2x60s)")

    config = AgentConfig(
        agent_id=f"eval_consolidation_{provider_name}",
        agent_type="consolidation",
        llm_model=model,
        max_retries=0,
        timeout_ms=agent_timeout_ms,
    )
    ctx = AgentExecutionContext(
        agent_type="consolidation",
        provider=adapter,
        model_name=model,
        tier=PerformanceTier.PERFORMANCE,
        capabilities=adapter.get_capabilities(),
        provider_name=provider_name,
    )

    agent = ConsolidationAgent(
        config=config,
        execution_context=ctx,
        repository=StubRepo(),
        embedding_service=None,
        fact_write_service=None,
        fact_management_port=None,
        prompt_builder=None,
    )
    # Override thinking effort if requested (instance attribute shadows class)
    if thinking_override is not None:
        agent.THINKING_EFFORT = thinking_override
        print(f"   thinking_effort override: {thinking_override}")

    # Override temperature if requested
    if temperature_override is not None:
        agent.TEMPERATURE = temperature_override
        print(f"   temperature override: {temperature_override}")

    # Wrap _call_llm at BaseAgent level to bump per-call parameters:
    #   - timeout: 500 → 1800s (only in flex mode, to match HTTP timeout —
    #     ConsolidationAgent hardcodes timeout=500 in LLMRequest)
    #   - max_tokens: only when --max-tokens is explicitly passed. Otherwise
    #     CONSOLIDATION.max_tokens from prod config flows through naturally.
    bump_timeout = flex
    bump_max_tokens = max_tokens_override is not None
    target_max_tokens = max_tokens_override or 0

    if bump_timeout or bump_max_tokens:
        original_call_llm = agent._call_llm

        async def call_llm_with_bumped_params(request, turn=0):
            updates: Dict[str, Any] = {}
            if bump_timeout:
                updates["timeout"] = 1800
            if bump_max_tokens:
                updates["max_tokens"] = target_max_tokens
            try:
                request = request.model_copy(update=updates)
            except AttributeError:
                for k, v in updates.items():
                    setattr(request, k, v)
            return await original_call_llm(request, turn=turn)

        agent._call_llm = call_llm_with_bumped_params  # type: ignore
        if bump_timeout:
            print(f"   timeout override: 1800s")
        if bump_max_tokens:
            print(f"   max_tokens override: {target_max_tokens}")

    # Reset billing counters (BaseAgent resets these in handle_message,
    # which we bypass by calling _run_consolidation_loop directly)
    agent._billing_prompt_tokens = 0
    agent._billing_completion_tokens = 0
    agent._billing_cache_read_tokens = 0
    agent._billing_cache_creation_tokens = 0

    cache_hits = 0
    cache_misses = 0
    miss_keys: List[Tuple[str, str]] = []
    captured_per_turn: List[List[Dict]] = []

    async def patched_execute(tool_calls, user_id, account_id):
        nonlocal cache_hits, cache_misses
        turn_capture: List[Dict] = []
        responses: List[ToolResponse] = []
        for tc in tool_calls:
            args_json = json.dumps(
                tc.args if isinstance(tc.args, dict) else {},
                sort_keys=True,
                ensure_ascii=False,
            )
            key = (tc.name, args_json)

            # search_existing_facts: ALWAYS return the unified fact pool from
            # the original session — ignore the actual query args. This makes
            # UPDATE decisions possible because the replayed provider sees the
            # real fact_ids that exist "in the database" (cumulatively, across
            # everything Claude found in this session).
            if tc.name == "search_existing_facts":
                result_str = unified_fact_pool
                hit = True
                cache_hits += 1
            elif key in cache:
                result_str = cache[key]
                cache_hits += 1
                hit = True
            else:
                result_str = synthetic_fallback(tc)
                cache_misses += 1
                miss_keys.append(key)
                hit = False
            turn_capture.append({
                "name": tc.name,
                "args": tc.args if isinstance(tc.args, dict) else {},
                "cache_hit": hit,
            })
            responses.append(ToolResponse(name=tc.name, result_str=result_str))
        captured_per_turn.append(turn_capture)
        return responses

    agent._execute_fact_management_tools = patched_execute  # type: ignore

    print(f"▶️  Replaying through {provider_name} ({model})...")
    t0 = time.time()
    error: Optional[str] = None
    operations: List[Dict] = []
    try:
        operations = await agent._run_consolidation_loop(
            user_message_text=initial_user_message,
            system_prompt=system_prompt,
            user_id="eval-user",
            account_id="eval-account",
        )
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        print(f"❌ {provider_name} failed: {error}")
    duration = time.time() - t0

    return {
        "provider": provider_name,
        "model": model,
        "duration_s": round(duration, 2),
        "tokens_prompt": agent._billing_prompt_tokens,
        "tokens_completion": agent._billing_completion_tokens,
        "tokens_total": (
            agent._billing_prompt_tokens + agent._billing_completion_tokens
        ),
        "tokens_cache_read": agent._billing_cache_read_tokens,
        "tokens_cache_creation": agent._billing_cache_creation_tokens,
        "operations": operations,
        "tool_calls_per_turn": captured_per_turn,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "miss_keys_sample": [
            {"name": n, "args": json.loads(a)} for n, a in miss_keys[:10]
        ],
        "turns": len(captured_per_turn) + (0 if error else 1),
        "error": error,
    }


# ----------------------------------------------------------------------------
# Comparison output
# ----------------------------------------------------------------------------

def summarize_operations(ops: List[Dict]) -> Dict[str, int]:
    counts = {"CREATE": 0, "UPDATE": 0, "MERGE": 0, "SUPERSEDE": 0, "DISCARD": 0}
    for op in ops:
        action = str(op.get("action", "")).upper()
        counts[action] = counts.get(action, 0) + 1
    return counts


def print_comparison_table(baseline: Dict, results: List[Dict]) -> None:
    headers = ("metric", "Claude (orig)") + tuple(r["provider"] for r in results)
    rows: List[Tuple] = [headers]

    rows.append((
        "model",
        baseline["claude_model"],
        *[r["model"] for r in results],
    ))
    rows.append((
        "turns",
        str(baseline["claude_turns"]),
        *[str(r["turns"]) for r in results],
    ))
    rows.append((
        "tokens (final turn)",
        str(baseline["claude_tokens"]),
        *[str(r["tokens_total"]) for r in results],
    ))
    rows.append((
        "  prompt tokens",
        "—",
        *[str(r["tokens_prompt"]) for r in results],
    ))
    rows.append((
        "  completion tokens",
        "—",
        *[str(r["tokens_completion"]) for r in results],
    ))
    rows.append((
        "  cache read",
        "—",
        *[str(r["tokens_cache_read"]) for r in results],
    ))
    rows.append((
        "  cache creation",
        "—",
        *[str(r["tokens_cache_creation"]) for r in results],
    ))
    rows.append((
        "duration (s)",
        "—",
        *[str(r["duration_s"]) for r in results],
    ))

    base_ops = summarize_operations(baseline["claude_operations"])
    for action in ("CREATE", "UPDATE", "MERGE", "SUPERSEDE", "DISCARD"):
        rows.append((
            action,
            str(base_ops.get(action, 0)),
            *[
                str(summarize_operations(r["operations"]).get(action, 0))
                for r in results
            ],
        ))

    rows.append((
        "ops total",
        str(len(baseline["claude_operations"])),
        *[str(len(r["operations"])) for r in results],
    ))
    rows.append((
        "cache hits",
        "—",
        *[str(r["cache_hits"]) for r in results],
    ))
    rows.append((
        "cache misses",
        "—",
        *[str(r["cache_misses"]) for r in results],
    ))

    col_widths = [
        max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))
    ]
    sep = "  |  "
    print("\n" + "=" * 80)
    print("CONSOLIDATION CROSS-PROVIDER COMPARISON")
    print("=" * 80)
    for r_i, row in enumerate(rows):
        line = sep.join(str(c).ljust(col_widths[i]) for i, c in enumerate(row))
        print(line)
        if r_i == 0:
            print("-" * len(line))
    print("=" * 80)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--session",
        required=True,
        help="Session prefix, e.g. 2026-04-10/2026-04-10_10-32-29",
    )
    parser.add_argument(
        "--providers",
        default="openai,gemini",
        help="Comma-separated provider list (openai,gemini)",
    )
    parser.add_argument(
        "--output",
        default="scripts/memory/eval_consolidation/",
        help="Output directory for JSON dumps",
    )
    parser.add_argument(
        "--openai-model",
        default=None,
        help="Override OpenAI model (default: PERFORMANCE tier)",
    )
    parser.add_argument(
        "--gemini-model",
        default=None,
        help="Override Gemini model (default: PERFORMANCE tier)",
    )
    parser.add_argument(
        "--thinking",
        default=None,
        choices=["low", "medium", "high"],
        help="Override thinking_effort for all replayed providers "
             "(default: keep CONSOLIDATION.thinking_effort='medium')",
    )
    parser.add_argument(
        "--flex",
        action="store_true",
        help="Enable Gemini Flex Inference (service_tier='flex', "
             "30min HTTP timeout, 2x60s retry on 429/503, max_tokens=65500). "
             "Only valid with --providers gemini.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Override LLMRequest.max_tokens (default: keep "
             "CONSOLIDATION.max_tokens=32_000). On Gemini 3 Pro, thinking "
             "tokens count against max_output_tokens — bump to 65500 to give "
             "thinking room. Auto-applied in --flex mode.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Override ConsolidationAgent.TEMPERATURE (default: keep "
             "CONSOLIDATION.temperature=1.0). Lower values reduce sampling "
             "noise — useful for reproducibility experiments.",
    )
    args = parser.parse_args()

    load_dotenv()

    output_dir = REPO_ROOT / args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"📥 Loading session {args.session}...")
    session_files = list_session_files(args.session)
    print(f"   Found {len(session_files)} files")
    for f in session_files:
        print(f"     {f.split('/')[-1]}")

    cache, baseline = build_replay_cache(session_files)
    unified_pool_str = build_unified_fact_pool(cache)
    unified_pool_count = len(json.loads(unified_pool_str))
    print(f"📦 Built cache: {len(cache)} entries")
    print(f"🧬 Unified fact pool: {unified_pool_count} unique facts "
          f"({len(unified_pool_str)} chars)")
    print(
        f"📊 Baseline: {baseline['claude_model']}, "
        f"{baseline['claude_turns']} turns, "
        f"{len(baseline['claude_operations'])} ops, "
        f"{baseline['claude_tokens']} tokens (final turn)"
    )

    # Save baseline
    (output_dir / "baseline_claude.json").write_text(
        json.dumps(
            {
                "model": baseline["claude_model"],
                "turns": baseline["claude_turns"],
                "tokens_final_turn": baseline["claude_tokens"],
                "operations": baseline["claude_operations"],
                "operations_breakdown": summarize_operations(
                    baseline["claude_operations"]
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    model_overrides = {
        "openai": args.openai_model,
        "gemini": args.gemini_model,
    }
    results: List[Dict[str, Any]] = []
    for provider in providers:
        result = await replay_through_provider(
            provider,
            baseline["system"],
            baseline["initial_user_message"],
            cache,
            unified_pool_str,
            model_override=model_overrides.get(provider),
            thinking_override=args.thinking,
            flex=args.flex,
            max_tokens_override=args.max_tokens,
            temperature_override=args.temperature,
        )
        results.append(result)
        out_file = output_dir / f"replay_{provider}.json"
        out_file.write_text(
            json.dumps(result, indent=2, ensure_ascii=False)
        )
        print(f"💾 Saved {out_file}")

    print_comparison_table(baseline, results)

    # Diff summary
    diff = {
        "session": args.session,
        "baseline": {
            "model": baseline["claude_model"],
            "operations_count": len(baseline["claude_operations"]),
            "operations_breakdown": summarize_operations(
                baseline["claude_operations"]
            ),
            "tokens_final_turn": baseline["claude_tokens"],
            "turns": baseline["claude_turns"],
        },
        "providers": [
            {
                "provider": r["provider"],
                "model": r["model"],
                "duration_s": r["duration_s"],
                "tokens_total": r["tokens_total"],
                "tokens_cache_read": r["tokens_cache_read"],
                "operations_count": len(r["operations"]),
                "operations_breakdown": summarize_operations(r["operations"]),
                "cache_hit_ratio": round(
                    r["cache_hits"] / max(1, r["cache_hits"] + r["cache_misses"]),
                    2,
                ),
                "cache_hits": r["cache_hits"],
                "cache_misses": r["cache_misses"],
                "turns": r["turns"],
                "error": r["error"],
            }
            for r in results
        ],
    }
    diff_file = output_dir / "diff_summary.json"
    diff_file.write_text(json.dumps(diff, indent=2, ensure_ascii=False))
    print(f"\n💾 Diff summary: {diff_file}")


if __name__ == "__main__":
    asyncio.run(main())
