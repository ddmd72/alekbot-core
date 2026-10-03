#!/usr/bin/env python3
"""
Agent model A/B: run a REAL specialist agent on REAL recent queries with model A vs model B.
=========================================================================================
Built for the GPT-6 evaluation (2026-10-03). It answers what a raw-prompt replay cannot,
for agents that use tools or grounding: the agent runs its own loop (grounded web search,
Maps tool calls) through the production adapter. Only `model_name` differs between the legs.

Legs:
  search_web — WebSearchAgent, grounded search (BALANCED tier in production)
  fetch_url  — WebSearchAgent, one known page (ECO downgrade in production); queries need a URL
  maps       — MapsSearchAgent, Maps tool loop (BALANCED)

Queries are the user's real recent delegations, pulled from BigQuery `prompt_content`.
Calls alternate A/B per query (A,B then B,A) so API latency drift hits both models evenly.

Output:
  * per-leg latency (p50/max), LLM calls, tokens, cost (priced by src/domain/billing.py)
  * a blind pairwise verdict per query from a Claude judge. The order of the two answers is
    randomized, and the judge sees no model names.
  * full answers in scripts/memory/ab_agents/ (gitignored — PII)

NOTE: real LLM + web/Maps calls on both models — spends API budget. Run deliberately.

Usage:
    python scripts/validation/ab_agent_models.py --leg search_web --a gpt-5.6-luna --b gpt-6-luna
    python scripts/validation/ab_agent_models.py --leg maps --a gpt-5.6-luna --b gpt-6-luna --n 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()

from google.cloud import firestore

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.composition.service_container import ServiceContainer
from src.composition.user_agent_factory import UserAgentFactory
from src.config.settings import load_settings
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.billing import calculate_cost
from src.domain.request_context import RequestContext
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.ports.llm_port import LLMRequest, Message, MessagePart

_OUT_DIR = Path("scripts/memory/ab_agents")
_LEG_SOURCE = {"search_web": "web_search", "fetch_url": "web_search", "maps": "maps_search"}
_LEG_AGENT = {"search_web": "web_search_agent", "fetch_url": "web_search_agent", "maps": "maps_search_agent"}
_JUDGE_MODEL = "claude-sonnet-5-5"
_URL = re.compile(r"https?://\S+")


def load_queries(leg: str, n: int, days: int) -> List[str]:
    """Most recent distinct user queries the given agent received (turn 0/1 = the delegation)."""
    project = os.environ["GCP_PROJECT_ID"] if os.getenv("GCP_PROJECT_ID") else os.environ["GOOGLE_CLOUD_PROJECT"]
    dataset = os.getenv("BIGQUERY_PROMPT_DATASET", "alek_observability_dev")
    sql = f"""
        SELECT request_text FROM `{project}.{dataset}.prompt_content`
        WHERE agent_type = '{_LEG_SOURCE[leg]}' AND turn <= 1
          AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        ORDER BY timestamp DESC LIMIT 400"""
    out = subprocess.run(
        ["bq", "query", f"--project_id={project}", "--use_legacy_sql=false", "--format=json",
         "--max_rows=400", sql], capture_output=True, text=True, check=True).stdout
    seen, queries = set(), []
    for row in json.loads(out):
        text = row["request_text"] or ""
        i = text.rfind("\nuser: ")
        if i < 0:
            continue
        q = text[i + len("\nuser: "):].strip()
        q = re.sub(r"^current_date_time:[^\n]*\n+", "", q)
        q = re.sub(r"^\[[^\]]*UTC\]\s*", "", q).strip()
        if leg == "fetch_url" and not _URL.search(q):
            continue
        if leg == "search_web" and _URL.search(q):
            continue
        key = q[:120]
        if q and key not in seen:
            seen.add(key)
            queries.append(q)
        if len(queries) >= n:
            break
    return queries


async def run_one(agent, leg: str, model: str, query: str, user_id: str, account_id: str) -> Dict[str, Any]:
    usage = {"calls": 0, "prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
    models_ran: List[str] = []
    orig = agent._call_llm

    async def capturing(request, turn=None):
        resp = await orig(request, turn=turn) if turn is not None else await orig(request)
        u = getattr(resp, "usage_metadata", None)
        ran = request.model_name
        models_ran.append(ran)
        if u:
            usage["calls"] += 1
            usage["prompt"] += u.prompt_tokens or 0
            usage["completion"] += u.completion_tokens or 0
            usage["cache_read"] += u.cache_read_tokens or 0
            usage["cache_write"] += u.cache_creation_tokens or 0
            usage["cost"] += calculate_cost(ran, u.prompt_tokens or 0, u.completion_tokens or 0,
                                            cache_read_tokens=u.cache_read_tokens or 0,
                                            cache_creation_tokens=u.cache_creation_tokens or 0)
        return resp

    agent._call_llm = capturing
    agent.model_name = model
    if leg == "fetch_url":
        agent._fetch_model_name = lambda: model
    payload: Dict[str, Any] = {"query": query}
    if leg == "fetch_url":
        url = _URL.search(query).group(0).rstrip(").,]")
        payload = {"url": url, "query": query.replace(url, "").strip()}
    message = AgentMessage.create(
        sender="ab_agent_models", recipient=agent.agent_id, intent=AgentIntent.QUERY,
        payload=payload, context={"user_id": user_id, "account_id": account_id, "session_id": "ab_agents"},
    )
    t0 = time.time()
    try:
        async with RequestContext(user_id=user_id, account_id=account_id):
            resp = await agent.execute(message)
        ok, answer = resp.status.value == "success", resp.result if isinstance(resp.result, str) else json.dumps(resp.result, ensure_ascii=False, default=str)
        if not ok:
            answer = f"[FAILED] {resp.error}"
    except Exception as e:  # noqa: BLE001 — a failed leg is a data point, not a crash
        ok, answer = False, f"[EXCEPTION] {type(e).__name__}: {e}"
    finally:
        agent._call_llm = orig
    return {"model": model, "models_ran": sorted(set(models_ran)), "ok": ok,
            "elapsed_s": round(time.time() - t0, 2), "answer": answer, **usage}


_JUDGE_SCHEMA = {"type": "object", "properties": {
    "better": {"type": "string", "enum": ["1", "2", "tie"]},
    "reason": {"type": "string"}}, "required": ["better", "reason"]}


async def judge(claude: ClaudeAdapter, query: str, a: str, b: str) -> Dict[str, Any]:
    """Blind pairwise judgement. Returns which of the ORIGINAL legs ('A'/'B'/'tie') won."""
    swap = random.random() < 0.5
    first, second = (b, a) if swap else (a, b)
    prompt = (
        "You compare two answers a research assistant gave to the same request. Judge them as "
        "the person who sent the request would: does it answer what was asked, is it accurate and "
        "specific (dates, places, numbers, sources), is it free of filler, does it admit what it "
        "could not find instead of inventing it. Length is not quality.\n\n"
        f"<request>\n{query}\n</request>\n\n<answer_1>\n{first[:12000]}\n</answer_1>\n\n"
        f"<answer_2>\n{second[:12000]}\n</answer_2>\n\n"
        "Return which answer is better (\"1\", \"2\" or \"tie\") and a one-sentence reason.")
    r = await claude.generate_content(LLMRequest(
        model_name=_JUDGE_MODEL, messages=[Message(role="user", parts=[MessagePart(text=prompt)])],
        response_schema=_JUDGE_SCHEMA, thinking="medium", max_tokens=4000))
    v = json.loads(r.text)
    winner = {"1": "B" if swap else "A", "2": "A" if swap else "B", "tie": "tie"}[v["better"]]
    return {"winner": winner, "reason": v["reason"]}


def summarize(rows: List[Dict[str, Any]], a: str, b: str) -> str:
    lines = []
    for leg_name, model in (("A", a), ("B", b)):
        legs = [r[leg_name] for r in rows]
        lat = sorted(x["elapsed_s"] for x in legs if x["ok"])
        lines.append(
            f"{leg_name} {model:14} ok={sum(x['ok'] for x in legs)}/{len(legs)} "
            f"p50={statistics.median(lat) if lat else float('nan'):.1f}s max={max(lat) if lat else float('nan'):.1f}s "
            f"calls={sum(x['calls'] for x in legs)} out_tok={sum(x['completion'] for x in legs)} "
            f"cost=${sum(x['cost'] for x in legs):.4f} ran={sorted({m for x in legs for m in x['models_ran']})}")
    wins = [r["judge"]["winner"] for r in rows if r.get("judge")]
    lines.append(f"judge ({_JUDGE_MODEL}, blind): A={wins.count('A')} B={wins.count('B')} tie={wins.count('tie')}")
    return "\n".join(lines)


async def main(args) -> None:
    user_id, account_id = os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"]
    queries = load_queries(args.leg, args.n, args.days)
    print(f"{len(queries)} real queries for leg={args.leg}")
    if not queries:
        return

    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=account_repo)
    factory = UserAgentFactory(config=settings, env_config=env_config, coordinator=AgentCoordinator(),
                               user_repo=user_repo, account_repo=account_repo, **container.agent_services())
    agent = (await factory.ensure_agents_for_user(user_id))[_LEG_AGENT[args.leg]]
    claude = ClaudeAdapter(api_key=os.environ["ANTHROPIC_API_KEY"].strip())

    rows: List[Dict[str, Any]] = []
    for i, q in enumerate(queries):
        order = ("A", "B") if i % 2 == 0 else ("B", "A")
        row: Dict[str, Any] = {"query": q}
        for leg_name in order:
            model = args.a if leg_name == "A" else args.b
            row[leg_name] = await run_one(agent, args.leg, model, q, user_id, account_id)
            r = row[leg_name]
            print(f"  [{i}] {leg_name} {model:14} {r['elapsed_s']:6.1f}s ok={r['ok']} calls={r['calls']} "
                  f"out={r['completion']} ${r['cost']:.4f}")
        if row["A"]["ok"] and row["B"]["ok"]:
            row["judge"] = await judge(claude, q, row["A"]["answer"], row["B"]["answer"])
            print(f"      judge → {row['judge']['winner']}: {row['judge']['reason'][:140]}")
        rows.append(row)

    summary = summarize(rows, args.a, args.b)
    print("\n" + summary)
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"{args.leg}_{args.a}_vs_{args.b}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"leg": args.leg, "a": args.a, "b": args.b, "summary": summary, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Full results → {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--leg", choices=sorted(_LEG_AGENT), required=True)
    ap.add_argument("--a", required=True, help="baseline model id")
    ap.add_argument("--b", required=True, help="candidate model id")
    ap.add_argument("--n", type=int, default=8, help="number of real queries")
    ap.add_argument("--days", type=int, default=14)
    asyncio.run(main(ap.parse_args()))
