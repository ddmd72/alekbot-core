"""Test adaptive WebSearch cognitive process (QUICK vs RESEARCH triage).

Assembles the system prompt from groovy token files (same as PromptBuilder),
runs 2 queries against Claude Haiku with web_search tool, and reports search statistics.

Usage:
  python scripts/prompt/test_websearch_adaptive.py
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from textwrap import indent

import anthropic


UPLOADS_DIR = Path("firestore_utils/uploads")
DOWNLOADS_DIR = Path("firestore_utils/downloads")

QUERIES = [
    "Найди хороший ресторан в El Puig",
    (
        "Find the exact OEM Mitsubishi part number for the fender-mounted side turn signal "
        "repeater for a European 2005 Mitsubishi Colt CZ3 1.3, VIN XMCMJZ34A6F016692. "
        "The previously suggested numbers D0816/D0817 and MN162980 appear incorrect. "
        "Search manufacturer parts catalogs, OEM databases, dealer EPC mirrors, and "
        "reliable parts sites. Need the exact OEM number, side applicability left/right, "
        "and fitment notes for this VIN/model. Do not provide generic advice; extract the "
        "actual part number if publicly available."
    ),
]


def _load_env() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _read_groovy(name: str) -> str:
    for d in (UPLOADS_DIR, DOWNLOADS_DIR):
        path = d / f"{name}.groovy"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"Token file {name}.groovy not found in uploads/ or downloads/")


def _assemble_prompt() -> str:
    """Assemble the full system prompt from groovy token files."""
    properties = _read_groovy("WEBSEARCH_PROPERTIES")
    cognitive = _read_groovy("WEBSEARCH_COGNITIVE_PROCESS")
    output_fmt = _read_groovy("WEBSEARCH_OUTPUT_FORMAT")
    execution = _read_groovy("WEBSEARCH_EXECUTION")

    now = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M %Z")

    sections = [
        ("properties", properties),
        ("cognitive_process", cognitive),
        ("output_format", output_fmt),
        ("execution", execution),
    ]

    body = "\n\n".join(
        f"    {name} {{\n\n{indent(content, '        ')}\n\n    }}"
        for name, content in sections
    )

    return (
        f"current_date_time: {now}\n\n"
        f"class WebSearchAgent extends Agent {{\n\n{body}\n\n}}"
    )


def main() -> None:
    _load_env()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (check .env)")

    client = anthropic.Anthropic(api_key=api_key)
    model = "claude-haiku-4-5-20251001"

    system_prompt = _assemble_prompt()

    print("=" * 80)
    print(f"MODEL: {model}")
    print("=" * 80)
    print("SYSTEM PROMPT")
    print("=" * 80)
    print(system_prompt)
    print("=" * 80)

    tools = [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 10,
        },
        {
            "type": "web_fetch_20250910",
            "name": "web_fetch",
            "max_uses": 5,
        },
        {
            "name": "respond",
            "description": "Return the search results in structured JSON format. MUST be called to deliver the final answer.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "Concrete fact found"},
                                "source": {"type": "string", "description": "Page title from search result"},
                                "url": {"type": "string", "description": "Exact URL from search result"},
                            },
                            "required": ["text", "source", "url"],
                        },
                    },
                    "conclusion": {"type": "string", "description": "2-3 sentence synthesis"},
                },
                "required": ["findings", "conclusion"],
            },
        },
    ]

    for i, query in enumerate(QUERIES, 1):
        print(f"\n{'=' * 80}")
        print(f"QUERY {i}: {query[:100]}{'...' if len(query) > 100 else ''}")
        print("=" * 80)

        messages = [{"role": "user", "content": query}]
        search_queries = []
        total_input = 0
        total_output = 0

        start = time.time()
        turn = 0

        while True:
            turn += 1
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=tools,
                tool_choice={"type": "any"},
                temperature=0.5,
            )

            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            # Collect search tool uses, results, and text blocks
            assistant_content = response.content
            block_types = [b.type for b in assistant_content]
            print(f"  [turn {turn}] blocks: {block_types}")
            for block in assistant_content:
                if block.type == "server_tool_use" and block.name == "web_search":
                    search_queries.append(block.input.get("query", ""))
                    print(f"  [turn {turn}] search: {block.input.get('query', '')}")
                elif block.type == "web_search_tool_result":
                    d = block.model_dump() if hasattr(block, "model_dump") else {}
                    for item in d.get("content", []):
                        if item.get("type") == "web_search_result":
                            print(f"             → {item.get('title', '')} | {item.get('url', '')}")

            # Check if model called our respond tool
            respond_result = None
            for block in assistant_content:
                if block.type == "tool_use" and block.name == "respond":
                    respond_result = block.input
                    break

            if respond_result:
                break

            # If end_turn without respond — force a second call to get structured JSON
            if response.stop_reason == "end_turn":
                # Append assistant prose as message, then force respond tool
                messages.append({"role": "assistant", "content": assistant_content})
                print(f"  [turn {turn}] end_turn without respond — forcing respond call")
                turn += 1
                response = client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                    tool_choice={"type": "tool", "name": "respond"},
                    temperature=0.5,
                )
                total_input += response.usage.input_tokens
                total_output += response.usage.output_tokens
                for block in response.content:
                    if block.type == "tool_use" and block.name == "respond":
                        respond_result = block.input
                        break
                break

            # If tool_use — could be respond (handled above) or needs continuation
            if response.stop_reason == "tool_use":
                if not respond_result:
                    messages.append({"role": "assistant", "content": assistant_content})
                    # Feed dummy result for non-server tools to continue
                    tool_uses = [b for b in assistant_content if b.type == "tool_use"]
                    if tool_uses:
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "tool_use_id": tu.id, "content": "ok"}
                                for tu in tool_uses
                            ],
                        })

            # Safety
            if response.stop_reason not in ("end_turn", "tool_use"):
                break

        duration = time.time() - start

        # Extract final output
        import json as _json

        print(f"\n--- Response ({duration:.1f}s, {turn} turn(s)) ---\n")

        if respond_result:
            print(_json.dumps(respond_result, ensure_ascii=False, indent=2))
        else:
            final_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    final_text += block.text
            print(final_text or "(empty)")

        print(f"\n--- Search Statistics ---")
        print(f"  Searches performed: {len(search_queries)}")
        if search_queries:
            print(f"  Search queries:")
            for sq in search_queries:
                print(f"    - {sq}")

        print(f"\n--- Tokens ---")
        print(f"  Input:  {total_input}")
        print(f"  Output: {total_output}")


if __name__ == "__main__":
    main()
