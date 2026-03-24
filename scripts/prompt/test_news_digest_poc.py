"""POC: Morning news digest — Claude native web search, per-topic orthogonal queries.

Uses Claude's native web_search_20250305 + web_fetch_20250910 tools (same as WebSearchAgent
via use_grounding=True in ClaudeAdapter). Claude decides how many searches to run based
on the cognitive process prompt.

Variants:
  v1 — per-topic analyst format (what happened + why it matters)
  v2 — v1 + cross-topic synthesis section

Usage:
    python scripts/prompt/test_news_digest_poc.py --topics "AI,крипто,геополітика"
    python scripts/prompt/test_news_digest_poc.py --topics "AI,Валенсія" --variant 2
    python scripts/prompt/test_news_digest_poc.py --prompt-file /tmp/prompt.txt --topics "AI"
    python scripts/prompt/test_news_digest_poc.py --topics "AI" --out /tmp/result.txt
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Variant 1: per-topic, 5 orthogonal vectors + fetch for depth, analyst format
# Based on COGNITIVE_PROCESS_WEBSEARCH with news-specific extensions.
# ---------------------------------------------------------------------------
SYSTEM_V1 = """\
properties {
  archetype: "Meticulous Multi-Vector News Analyst. Decomposes every topic into independent
              search dimensions. Fetches full article text before summarising. Never invents
              facts, never invents URLs. Cuts through noise to what actually changed today."
}

news_digest_task {
  "Given a list of topics, produce a morning analyst briefing for each topic.
   Focus exclusively on news from the LAST 48 HOURS.
   For each topic: search deeply, fetch the most relevant articles in full,
   then synthesise — not headlines, but what changed and specifically why it matters."
}

cognitive_process {
  rules: [
    "For EACH topic independently:",
    "  1. DECOMPOSE: devise exactly 5 ORTHOGONAL search vectors — maximally independent
          dimensions derived from THIS topic, no preset categories.
          Query language: primary language of the region for local topics
          (Spanish for Spain/Valencia, Ukrainian for Ukraine/Kyiv),
          English for global/tech topics (AI, crypto, science).
          Every query must include a date anchor: today's date or 'last 24 hours'.",
    "  2. SEARCH: run each of the 5 vectors as a separate search. Results do not cross vectors.",
    "  3. RECENCY CHECK: discard any result older than 48 hours. If all results are stale —
          mark topic as no recent news and move on.",
    "  4. FETCH: for the 2-3 most promising URLs found, use web_fetch to read the full article.
          Do not summarise from search snippets alone — fetch and read before writing.",
    "  5. SYNTHESISE: identify 1-2 most significant stories from the fetched content.
          Combine all vectors into one narrative per story — do not list vectors separately.",
    "  6. WRITE: for each story:
          • What happened — 3-4 sentences, concrete facts (numbers, names, dates, quotes).
            Draw on full article text fetched in step 4.
          • Why it matters — 2 sentences. Name the specific consequence, risk, or shift.
            Generic phrases ('this is significant', 'this is important') are not acceptable.",
    "Series data (prices, counts, dates, scores) — enumerate each item. Never collapse to ranges."
  ]
}

output_format {
  language: "same as the topics list"

  structure: [
    "## Morning Briefing — {DATE}",
    "",
    "### [Topic name]",
    "**[Story headline]** — [what happened, 3-4 sentences with concrete facts].",
    "[Why it matters — 2 sentences, specific consequence or shift].",
    "→ [Source title](url)",
    "",
    "### [Next topic]",
    "..."
  ]

  rules: [
    "Only news from the last 48 hours. Older content = skip entirely.",
    "No background paragraphs — assume the reader knows the domain basics.",
    "If no recent news for a topic: '### [Topic]\\nNo major developments in the last 48 hours.'",
    "Every story must end with → [Source title](real_url_from_fetch_or_search).",
    "If a story has no verifiable URL — omit the story entirely.",
    "Never invent or guess URLs.",
    "Mark unverifiable claims with _(unverified)_ before the source link.",
    "No closing summary paragraph."
  ]
}
"""

# ---------------------------------------------------------------------------
# Variant 2: v1 + cross-topic synthesis
# ---------------------------------------------------------------------------
SYSTEM_V2 = SYSTEM_V1 + """
cross_topic {
  "After all topic sections, add a separator and a cross-topic paragraph IF there is
   a meaningful connection between two or more topics (shared cause, shared implication,
   connected events). If no real connection — omit entirely, do not force it."

  format:
    "---"
    "**Cross-topic**: [2-4 sentences naming the connection and the specific topics involved]."
}
"""

VARIANTS = {"1": SYSTEM_V1, "2": SYSTEM_V2}


def _load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run(topics: str, model: str, variant: str, prompt_file: str | None = None) -> None:
    import anthropic

    if prompt_file:
        system_instruction = Path(prompt_file).read_text(encoding="utf-8")
    else:
        system_instruction = VARIANTS[variant]

    current_date = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M UTC")
    system_instruction = f"current_date_time: {current_date}\n\n{system_instruction}"

    user_message = f"Topics: {topics}"

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    print(f"\nTopics  : {topics}")
    print(f"Model   : {model}")
    print(f"Variant : {prompt_file or variant}")
    print("=" * 60)

    start = time.time()

    # Replicates ClaudeAdapter use_grounding=True behaviour exactly:
    # - web_search_20250305 + web_fetch_20250910 tools
    # - web-search-2025-03-05 beta header
    response = client.beta.messages.create(
        model=model,
        max_tokens=16_000,
        system=system_instruction,
        messages=[{"role": "user", "content": user_message}],
        tools=[
            {"type": "web_search_20250305", "name": "web_search"},
            {"type": "web_fetch_20250910",  "name": "web_fetch"},
        ],
        betas=["web-search-2025-03-05"],
        temperature=0.5,
    )

    elapsed = time.time() - start

    # Extract final text and count tool calls
    text_parts = []
    tool_calls = 0
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(block.text)
        elif block_type in ("tool_use", "tool_result"):
            tool_calls += 1

    # For extended thinking / multi-turn tool use, Claude returns all turns inline
    text = "\n".join(text_parts).strip()

    print(text or "[no text in response]")
    print(f"\n[timing] {elapsed:.2f}s")
    print(f"[tool_calls] {tool_calls}")
    print(f"[stop_reason] {response.stop_reason}")
    usage = getattr(response, "usage", None)
    if usage:
        print(f"[tokens] input={getattr(usage, 'input_tokens', '?')} output={getattr(usage, 'output_tokens', '?')}")


def main() -> None:
    _load_env(Path(".env"))

    parser = argparse.ArgumentParser(description="News digest POC — Claude native web search")
    parser.add_argument(
        "--topics",
        default="AI news, Valencia Spain events, Ukraine war",
        help='Comma-separated topic list',
    )
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument(
        "--variant",
        default="1",
        choices=["1", "2"],
        help="1=analyst format, 2=analyst + cross-topic synthesis",
    )
    parser.add_argument("--prompt-file", metavar="FILE")
    parser.add_argument("--out", metavar="FILE", help="Save output to file")
    args = parser.parse_args()

    kwargs = dict(
        topics=args.topics,
        model=args.model,
        variant=args.variant,
        prompt_file=args.prompt_file,
    )

    if args.out:
        import io
        import sys

        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        run(**kwargs)
        sys.stdout = old_stdout
        content = buf.getvalue()
        print(content)
        Path(args.out).write_text(content, encoding="utf-8")
        print(f"\n[saved] {args.out}")
    else:
        run(**kwargs)


if __name__ == "__main__":
    main()
