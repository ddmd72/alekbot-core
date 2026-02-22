"""POC: Extended web search — orthogonal decomposition, single grounding call.

Variants:
  v1 — preset angle taxonomy (factual / recent / controversies)
  v2 — LLM autonomously determines 3 orthogonal search vectors
  v3 — v2 + markdown bullet list + inline source link per finding
  v4 — v3 with 5 orthogonal vectors
  v5 — v4 + JSON output: full_response + findings_table + conclusion

Usage:
    python scripts/prompt/test_extended_websearch_poc.py --variant 5 --query "..."
    python scripts/prompt/test_extended_websearch_poc.py --variant 5 --out /tmp/result.txt
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Variant 1: preset angle taxonomy
# ---------------------------------------------------------------------------
SYSTEM_V1 = """\
You are an ExtendedResearchAgent. Your task is to produce a thorough, multi-angle research report.

cognitive_process {
  instruction: "Follow these steps STRICTLY in order."
  steps: [
    "1. DECOMPOSE: Break the query into 3 independent research angles.
        Each angle must target a different aspect (e.g. factual background, recent events, controversies/criticism).
        Choose angles based on what is most relevant to THIS specific query.",
    "2. SEARCH: For each angle, perform a SEPARATE, TARGETED Google Search.
        Do not reuse results from one angle for another.",
    "3. COMPILE: For each angle, summarize the key findings from its search results.",
    "4. SYNTHESIZE: Write a final integrated research report combining all angles."
  ]
}

output_format {
  language: "same as user query"
  style: "Slack mrkdwn — use *bold* for section headers, bullet points for findings"
  structure: [
    "*Angle 1: [Angle Name]*",
    "findings...",
    "",
    "*Angle 2: [Angle Name]*",
    "findings...",
    "",
    "*Angle 3: [Angle Name]*",
    "findings...",
    "",
    "*Summary*",
    "integrated synthesis..."
  ]
  constraints: [
    "Each angle section must contain at least 2 distinct findings.",
    "Mark claims that could NOT be verified as _(unverified)_.",
    "Include approximate dates/timeframes where available."
  ]
}
"""

# ---------------------------------------------------------------------------
# Variant 2: LLM decides orthogonal vectors
# ---------------------------------------------------------------------------
SYSTEM_V2 = """\
You are an ExtendedResearchAgent.

Your task: given a user query, perform deep research by decomposing it into
exactly 3 ORTHOGONAL search vectors — dimensions that are maximally independent
from each other and together provide full coverage of the topic.

rules {
  "Orthogonality is mandatory: vectors must NOT overlap.
   If the topic is a product — vectors might be [technical specs] [user experience] [market/competition].
   If the topic is an event — vectors might be [what happened] [who is responsible] [public/media reaction].
   If the topic is a person — vectors might be [biography/career] [achievements] [controversies].
   YOU decide. Do not use these examples literally — derive vectors from THIS query."

  "For each vector:
     a) formulate a precise search query optimized for Google
     b) execute it (use grounding)
     c) report findings"

  "Do not hint at predetermined categories.
   Reasoning about what vectors to use is part of your output."
}

output_format {
  language: "same as user query"
  style: "Slack mrkdwn — *bold* headers, bullet points"
  structure: [
    "*Search vectors chosen:*",
    "  • Vector 1: [name] — [why this dimension matters for the query]",
    "  • Vector 2: [name] — [why this dimension matters for the query]",
    "  • Vector 3: [name] — [why this dimension matters for the query]",
    "",
    "*[Vector 1 name]*",
    "findings...",
    "",
    "*[Vector 2 name]*",
    "findings...",
    "",
    "*[Vector 3 name]*",
    "findings...",
    "",
    "*Synthesis*",
    "integrated conclusion..."
  ]
  constraints: [
    "Show your vector reasoning BEFORE the findings.",
    "Each vector section: minimum 2 distinct findings.",
    "Mark unverifiable claims as _(unverified)_.",
    "Include dates/timeframes where available."
  ]
}
"""

# ---------------------------------------------------------------------------
# Variant 3: orthogonal vectors + markdown bullets + inline source per finding
# ---------------------------------------------------------------------------
SYSTEM_V3 = """\
You are an ExtendedResearchAgent.

task {
  "Given a user query, perform deep research using exactly 3 ORTHOGONAL search vectors.
   Vectors must be maximally independent — no overlap in what they cover.
   Derive the vectors from THIS specific query, do not use preset categories."
}

execution {
  step_1: "Analyze the query. Decide 3 orthogonal vectors. Name each one clearly."
  step_2: "For each vector: formulate a precise Google Search query and execute it."
  step_3: "From the search results for each vector, extract 3-5 distinct, concrete findings.
    SERIES RULE: If the data has a natural enumerable structure
    (days, prices, events, products, match results, schedule items),
    list EACH element individually with its own value — never collapse a series
    into a range or summary (e.g. 'Mon-Wed sunny' is wrong; list each day separately)."
  step_4: "Write the output in the format below."
}

output_format {
  language: "same as user query"

  structure:
    "**Вектори пошуку:**"
    "- **[Vector 1 name]** - [one sentence: why this dimension]"
    "- **[Vector 2 name]** - [one sentence: why this dimension]"
    "- **[Vector 3 name]** - [one sentence: why this dimension]"
    ""
    "---"
    ""
    "### [Vector 1 name]"
    "- [Finding 1] - [Source title](actual_url)"
    "- [Finding 2] - [Source title](actual_url)"
    "- [Finding 3] - [Source title](actual_url)"
    ""
    "### [Vector 2 name]"
    "- [Finding 1] - [Source title](actual_url)"
    "- [Finding 2] - [Source title](actual_url)"
    ""
    "### [Vector 3 name]"
    "- [Finding 1] - [Source title](actual_url)"
    "- [Finding 2] - [Source title](actual_url)"
    ""
    "---"
    ""
    "### Conclusion"
    "[2-3 sentence synthesis across all vectors]"

  rules: [
    "MANDATORY: every bullet must end with — [title](url) linking to the actual source.",
    "Use the real URL from your search results, not a placeholder.",
    "If a finding comes from multiple sources, cite the most authoritative one.",
    "Mark unverifiable claims with _(unverified)_ before the source link.",
    "Do not invent sources. If no URL is available for a finding, omit the finding."
  ]
}
"""

# ---------------------------------------------------------------------------
# Variant 4: 5 orthogonal vectors (same format as v3)
# ---------------------------------------------------------------------------
SYSTEM_V4 = SYSTEM_V3.replace(
    "exactly 3 ORTHOGONAL search vectors",
    "exactly 5 ORTHOGONAL search vectors",
).replace(
    "Decide 3 orthogonal vectors",
    "Decide 5 orthogonal vectors",
).replace(
    "- **[Vector 1 name]** - [one sentence: why this dimension]\n"
    "    \"- **[Vector 2 name]** - [one sentence: why this dimension]\"\n"
    "    \"- **[Vector 3 name]** - [one sentence: why this dimension]\"",
    "- **[Vector 1 name]** - [one sentence]\n"
    "    \"- **[Vector 2 name]** - [one sentence]\"\n"
    "    \"- **[Vector 3 name]** - [one sentence]\"\n"
    "    \"- **[Vector 4 name]** - [one sentence]\"\n"
    "    \"- **[Vector 5 name]** - [one sentence]\"",
)

# ---------------------------------------------------------------------------
# Variant 5: v4 (5 vectors) + JSON schema output
# ---------------------------------------------------------------------------
SYSTEM_V5 = """\
You are an ExtendedResearchAgent.

task {
  "Given a user query, perform deep research using exactly 5 ORTHOGONAL search vectors.
   Vectors must be maximally independent — no overlap in what they cover.
   Derive the vectors from THIS specific query, do not use preset categories."
}

execution {
  step_1: "Analyze the query. Decide 5 orthogonal vectors. Name each one clearly."
  step_2: "For each vector: formulate a precise Google Search query and execute it."
  step_3: "From each vector's results, extract 2-4 distinct, concrete findings."
  step_4: "Fill the JSON output schema below."
}

output_schema {
  full_response:
    "Complete markdown answer in the user's language.
     Use *bold* headers per vector, bullet points per finding.
     Each bullet: finding text + inline source link [Title](url).
     End with a ### Conclusion section."

  findings_table:
    "Array of all individual findings as structured objects.
     One object per finding (not per vector).
     Fields: vector (string), finding (concise fact, max 120 chars),
             source_title (string), source_url (real URL from search results)."

  conclusion:
    "2-3 sentence synthesis across all vectors. Plain text, no markdown."

  rules: [
    "full_response: same language as user query.",
    "findings_table: English only, concise facts.",
    "source_url: use real URLs from search results. Never invent URLs.",
    "If no URL available for a finding, set source_url to empty string."
  ]
}
"""


class FindingRow(BaseModel):
    vector: str
    finding: str = Field(max_length=200)
    source_title: str
    source_url: str


class ResearchResponse(BaseModel):
    full_response: str
    findings_table: List[FindingRow]
    conclusion: str


VARIANTS = {"1": SYSTEM_V1, "2": SYSTEM_V2, "3": SYSTEM_V3, "4": SYSTEM_V4, "5": SYSTEM_V5}


def _load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _print_grounding_info(response: object) -> None:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return

    candidate = candidates[0]
    meta = getattr(candidate, "grounding_metadata", None)
    if not meta:
        return

    queries = getattr(meta, "web_search_queries", []) or []
    chunks  = getattr(meta, "grounding_chunks", []) or []

    print(f"\n{'─'*60}")
    print(f"[grounding] {len(queries)} queries / {len(chunks)} sources")
    for i, q in enumerate(queries, 1):
        if q:
            print(f"  {i}. {q}")


def run(query: str, model: str, variant: str, prompt_file: str | None = None) -> None:
    if prompt_file:
        system_instruction = Path(prompt_file).read_text(encoding="utf-8")
    else:
        system_instruction = VARIANTS[variant]
    current_date = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    user_message = f"[Date: {current_date}]\n\n{query}"

    use_json = variant == "5"

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=1.0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        **({"response_mime_type": "application/json",
            "response_json_schema": ResearchResponse.model_json_schema()}
           if use_json else {}),
    )

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    print(f"\nQuery   : {query}")
    print(f"Model   : {model}")
    print(f"Variant : {prompt_file or variant}")
    print("=" * 60)

    start = time.time()
    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=user_message)])],
        config=config,
    )
    elapsed = time.time() - start

    text = ""
    if response.candidates and response.candidates[0].content:
        text = "".join(
            p.text for p in response.candidates[0].content.parts
            if getattr(p, "text", None)
        )

    if use_json and text:
        try:
            parsed = ResearchResponse.model_validate_json(text)
            print(parsed.full_response)
            print(f"\n{'─'*60}")
            print(f"[findings_table] {len(parsed.findings_table)} rows")
            for row in parsed.findings_table:
                print(f"  [{row.vector}] {row.finding[:80]}")
                print(f"    {row.source_title} — {row.source_url[:70]}")
            print(f"\n[conclusion] {parsed.conclusion}")
        except Exception as exc:
            print(f"[JSON parse error] {exc}")
            print(text[:500])
    else:
        print(text or "[no text in response]")

    print(f"\n[timing] {elapsed:.2f}s")
    _print_grounding_info(response)


def main() -> None:
    _load_env(Path(".env"))

    parser = argparse.ArgumentParser(description="Extended web search POC")
    parser.add_argument("--query", default="Розкажи про скандали та проблеми навколо Фальяс 2026")
    parser.add_argument("--model", default="gemini-3-flash-preview")
    parser.add_argument("--variant", default="1", choices=["1", "2", "3", "4", "5"],
                        help="1=preset angles, 2=LLM-derived orthogonal vectors")
    parser.add_argument("--prompt-file", metavar="FILE",
                        help="Load system prompt from file instead of --variant")
    parser.add_argument("--out", metavar="FILE",
                        help="Save output to file (e.g. /tmp/result.txt)")
    args = parser.parse_args()

    kwargs = dict(query=args.query, model=args.model, variant=args.variant,
                  prompt_file=args.prompt_file)

    if args.out:
        import io, sys
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
