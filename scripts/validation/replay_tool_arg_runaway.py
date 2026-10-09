"""Replay a stored OpenAI Responses call to test the tool-argument whitespace runaway.

Hypothesis (2026-10-09): under strict function-calling grammar, the model wants to emit a
`context` key the schema does not declare (the manifest's payload prose names it, the tool
schema does not), the grammar masks that token, and the model can only emit whitespace until
max_output_tokens. This script re-runs the exact stored request (instructions, tools, input,
reasoning) in arms that differ only in the delegate_to_specialist schema / strict flag.

Usage:
    venv/bin/python scripts/validation/replay_tool_arg_runaway.py dump <response_id>
    venv/bin/python scripts/validation/replay_tool_arg_runaway.py run <response_id> \
        --arms A,B,C --n 5 --max-output 8000

Output (may contain user data) goes to scripts/memory/ (gitignored) only.
"""
import argparse
import asyncio
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "scripts" / "memory" / "tool_arg_runaway"
WS_RUN = re.compile(r"\s{200,}")


def _client() -> AsyncOpenAI:
    load_dotenv(ROOT / ".env")
    # trust_env=False: bypass a local MITM proxy (Charles) picked up from macOS system settings.
    return AsyncOpenAI(
        api_key=os.environ["OPENAI_API_KEY"].strip(), timeout=900.0, max_retries=0,
        http_client=httpx.AsyncClient(trust_env=False, timeout=900.0),
    )


async def dump(response_id: str) -> Path:
    client = _client()
    resp = await client.responses.retrieve(response_id)
    items = []
    async for item in client.responses.input_items.list(response_id, order="asc", limit=100):
        items.append(item.model_dump(exclude_none=True))
    data = {
        "response_id": response_id,
        "model": resp.model,
        "instructions": resp.instructions,
        "tools": [t.model_dump(exclude_none=True) for t in (resp.tools or [])],
        "tool_choice": resp.tool_choice if isinstance(resp.tool_choice, str) else resp.tool_choice.model_dump(),
        "reasoning": resp.reasoning.model_dump(exclude_none=True) if resp.reasoning else None,
        "text": resp.text.model_dump(exclude_none=True) if resp.text else None,
        "max_output_tokens": resp.max_output_tokens,
        "status": resp.status,
        "usage": resp.usage.model_dump() if resp.usage else None,
        "input_items": items,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{response_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    types = {}
    for it in items:
        types[it.get("type", "?")] = types.get(it.get("type", "?"), 0) + 1
    print(f"saved {path}")
    print(f"model={resp.model} status={resp.status} max_output_tokens={resp.max_output_tokens}")
    print(f"reasoning={data['reasoning']} text={data['text']} tool_choice={data['tool_choice']}")
    print(f"input item types: {types}")
    for t in data["tools"]:
        print(f"tool: name={t.get('name')} type={t.get('type')} strict={t.get('strict')}")
    return path


def _clean_input(items: list) -> list:
    """Stored input items → request input. Drop server ids/status; keep content."""
    out = []
    for it in items:
        it = copy.deepcopy(it)
        it.pop("id", None)
        it.pop("status", None)
        if it.get("role") == "assistant" and isinstance(it.get("content"), list):
            # The stored item round-trips through the SDK model as input_text, which the API
            # rejects for assistant turns; a plain string is the equivalent wire form.
            it["content"] = "".join(p.get("text", "") for p in it["content"])
        out.append(it)
    return out


def _arm_tools(tools: list, arm: str) -> list:
    tools = copy.deepcopy(tools)
    for t in tools:
        if t.get("type") != "function" or t.get("name") != "delegate_to_specialist":
            continue
        params = t["parameters"]
        ctx = params["properties"]["context"]
        if arm == "B":
            props = ctx.setdefault("properties", {})
            props["language"] = {"type": "string", "description": "Report language (ISO 639-1)."}
            props["brief"] = {"type": "string", "description": "One-sentence summary, max 400 chars."}
            if isinstance(ctx.get("required"), list):
                ctx["required"] += ["language", "brief"]
        elif arm == "C":
            t["strict"] = False
    return tools


def _classify(resp) -> dict:
    args = []
    for item in resp.output or []:
        if getattr(item, "type", "") == "function_call":
            args.append(item.arguments or "")
    joined = "\n".join(args)
    m = WS_RUN.search(joined)
    parse_ok = True
    for a in args:
        try:
            json.loads(a)
        except Exception:
            parse_ok = False
    intents = re.findall(r'"intent"\s*:\s*"([a-z_]+)"', joined)
    return {
        "status": resp.status,
        "out_tokens": resp.usage.output_tokens if resp.usage else None,
        "reasoning_tokens": (resp.usage.output_tokens_details.reasoning_tokens
                             if resp.usage and resp.usage.output_tokens_details else None),
        "n_calls": len(args),
        "intents": intents,
        "args_len": len(joined),
        "ws_run_at": m.start() if m else None,
        "ws_context": joined[max(0, m.start() - 160): m.start() + 20] if m else None,
        "parse_ok": parse_ok,
        "context_keys": [list(json.loads(a).get("context", {}).keys()) for a in args if _safe(a)],
        "context_nonempty": [{k: v for k, v in json.loads(a).get("context", {}).items() if v}
                             for a in args if _safe(a)],
        "query_tail": [json.loads(a).get("query", "")[-300:] for a in args if _safe(a)],
        "mentions_lang_brief_in_query": [bool(re.search(r"(?i)language|brief", json.loads(a).get("query", "")[-1500:]))
                                         for a in args if _safe(a)],
    }


def _safe(a: str) -> bool:
    try:
        json.loads(a)
        return True
    except Exception:
        return False


async def _one(client, data, arm, i, max_output):
    kwargs = dict(
        model=data["model"],
        instructions=data["instructions"],
        input=_clean_input(data["input_items"]),
        tools=_arm_tools(data["tools"], arm),
        tool_choice=data["tool_choice"],
        max_output_tokens=max_output,
        store=False,
    )
    if data.get("reasoning"):
        kwargs["reasoning"] = {k: v for k, v in data["reasoning"].items() if k in ("effort", "summary")}
    if data.get("text") and data["text"].get("format"):
        fmt = dict(data["text"]["format"])
        if "schema_" in fmt:  # SDK model_dump alias → wire name
            fmt["schema"] = fmt.pop("schema_")
        kwargs["text"] = {"format": fmt}
    t0 = time.monotonic()
    try:
        resp = await client.responses.create(**kwargs)
        res = _classify(resp)
    except Exception as e:  # report, keep the other runs going
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    res.update({"arm": arm, "i": i, "seconds": round(time.monotonic() - t0, 1)})
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return res


async def run(response_id: str, arms: list, n: int, max_output: int, concurrency: int):
    path = OUT_DIR / f"{response_id}.json"
    if not path.exists():
        await dump(response_id)
    data = json.loads(path.read_text())
    client = _client()
    sem = asyncio.Semaphore(concurrency)

    async def guarded(arm, i):
        async with sem:
            return await _one(client, data, arm, i, max_output)

    results = await asyncio.gather(*(guarded(a, i) for a in arms for i in range(n)))
    out = OUT_DIR / f"{response_id}_runs_{int(time.time())}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\nsaved {out}")
    for arm in arms:
        rs = [r for r in results if r["arm"] == arm]
        ws = sum(1 for r in rs if r.get("ws_run_at") is not None)
        err = sum(1 for r in rs if "error" in r)
        print(f"arm {arm}: runs={len(rs)} whitespace_runaway={ws} errors={err}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["dump", "run"])
    p.add_argument("response_id")
    p.add_argument("--arms", default="A,B,C")
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--max-output", type=int, default=8000)
    p.add_argument("--concurrency", type=int, default=5)
    a = p.parse_args()
    if a.cmd == "dump":
        asyncio.run(dump(a.response_id))
    else:
        asyncio.run(run(a.response_id, a.arms.split(","), a.n, a.max_output, a.concurrency))


if __name__ == "__main__":
    sys.exit(main())
