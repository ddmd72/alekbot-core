"""Test adaptive WebSearch cognitive process on OpenAI web_search.

Uses Responses API with native web_search tool.
Extracts annotations (url_citation) from responses.

Usage:
  python scripts/prompt/test_websearch_openai.py
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from textwrap import indent

import openai


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
    properties = _read_groovy("WEBSEARCH_PROPERTIES")
    cognitive = _read_groovy("WEBSEARCH_COGNITIVE_PROCESS")
    output_fmt = _read_groovy("WEBSEARCH_OUTPUT_FORMAT")

    now = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M %Z")

    sections = [
        ("properties", properties),
        ("cognitive_process", cognitive),
        ("output_format", output_fmt),
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

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set (check .env)")

    client = openai.OpenAI(api_key=api_key)
    model = "gpt-4o-mini-search-preview"

    system_prompt = _assemble_prompt()

    print("=" * 80)
    print(f"MODEL: {model}")
    print("=" * 80)
    print("SYSTEM PROMPT")
    print("=" * 80)
    print(system_prompt)
    print("=" * 80)

    for i, query in enumerate(QUERIES, 1):
        print(f"\n{'=' * 80}")
        print(f"QUERY {i}: {query[:100]}{'...' if len(query) > 100 else ''}")
        print("=" * 80)

        start = time.time()

        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            web_search_options={},
        )

        duration = time.time() - start

        choice = completion.choices[0]
        message = choice.message
        text = message.content or ""

        # Extract annotations
        annotations = getattr(message, "annotations", None) or []
        url_citations = []
        for ann in annotations:
            ann_type = getattr(ann, "type", None)
            if ann_type == "url_citation":
                url_citations.append({
                    "url": getattr(ann, "url", ""),
                    "title": getattr(ann, "title", ""),
                    "start_index": getattr(ann, "start_index", 0),
                    "end_index": getattr(ann, "end_index", 0),
                })

        print(f"\n--- Response ({duration:.1f}s) ---\n")
        print(text)

        if url_citations:
            print(f"\n--- URL Citations ({len(url_citations)}) ---")
            for c in url_citations:
                print(f"  [{c['start_index']}:{c['end_index']}] {c['title']}")
                print(f"    {c['url']}")

        # Token usage
        if completion.usage:
            print(f"\n--- Tokens ---")
            print(f"  Prompt:     {completion.usage.prompt_tokens}")
            print(f"  Completion: {completion.usage.completion_tokens}")
            print(f"  Total:      {completion.usage.total_tokens}")


if __name__ == "__main__":
    main()
