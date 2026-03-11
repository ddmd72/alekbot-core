"""POC: Gemini web search with strict JSON output.

Runs 5 test queries against gemini-flash-latest with Google Search grounding
and saves responses to reports/websearch_poc_results.json.

Usage:
  GEMINI_API_KEY=... python scripts/prompt/test_websearch_poc.py
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


def build_system_instruction(current_date: str) -> str:
    return f"""You are a Google Search agent.

CURRENT DATE: {current_date}

BEHAVIOR:
- User message is ALWAYS a search query
- Execute Google Search
- Return results in English (regardless of query language)
- Use Slack markdown: *bold*, _italic_
- history_summary: Brief factual summary max 150 chars (no "Answered/Showed", just the fact)
- rich_content: Include ONLY for naturally tabular data

USE rich_content FOR:
✓ Weather forecast (3+ days)
✓ Event listings (3+ items)
✓ Comparison data
✓ Schedules/timetables

EXAMPLES:

Query: "Погода в Валенсии на неделю"
{{
  "full_response": "*Weather forecast for Valencia:*\\n\\nMonday-Wednesday sunny 16-19°C. Thursday rain, 14°C. Weekend cooler with clouds.",
  "history_summary": "Valencia weather: Mon-Wed sunny 16-19°C, Thu rain 14°C, weekend cloudy 12-16°C",
  "rich_content": {{
    "type": "table",
    "columns": ["Day", "Temp", "Condition", "Humidity", "Wind"],
    "rows": [
      ["Monday", "14-19°C", "☀️ Sunny", "65%", "10 km/h"],
      ["Tuesday", "15-18°C", "⛅ Partly Cloudy", "70%", "12 km/h"]
    ]
  }}
}}

Query: "Who is president of France?"
{{
  "full_response": "The President of France is Emmanuel Macron. He has held this position since May 2017.",
  "history_summary": "President of France: Emmanuel Macron (since May 2017)"
}}

Query: "Concerts in Barcelona February"
{{
  "full_response": "*Concerts in Barcelona - February 2026:*\\n\\n*Coldplay* — Feb 10, Palau Sant Jordi\\n*Billie Eilish* — Feb 15, Primavera Sound\\n*Rosalía* — Feb 22, Sala Apolo\\n*The Weeknd* — Feb 28, Estadi Olímpic",
  "history_summary": "Barcelona Feb 2026: Coldplay (10th), Billie Eilish (15th), Rosalía (22nd), The Weeknd (28th)",
  "rich_content": {{
    "type": "table",
    "columns": ["Artist", "Date", "Venue"],
    "rows": [
      ["Coldplay", "Feb 10", "Palau Sant Jordi"],
      ["Billie Eilish", "Feb 15", "Primavera Sound"]
    ]
  }}
}}

Execute search for the user's query.
"""


class RichContent(BaseModel):
    type: str = Field(description="table or list")
    columns: Optional[List[str]] = Field(default=None, description="Column headers")
    rows: Optional[List[List[str]]] = Field(default=None, description="Row data")


class WebSearchResponse(BaseModel):
    full_response: str = Field(description="Complete answer in English")
    history_summary: str = Field(description="Brief factual summary max 150 chars")
    rich_content: Optional[RichContent] = None


def extract_text(response: Any) -> str:
    if not getattr(response, "candidates", None):
        return ""
    candidate = response.candidates[0]
    if not candidate.content or not candidate.content.parts:
        return ""
    return "".join([p.text for p in candidate.content.parts if p.text])


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> None:
    _load_env_file(Path(".env"))

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (check .env)")

    client = genai.Client(api_key=api_key)
    model_name = "gemini-3-flash-preview"

    current_date = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M %Z")
    system_instruction = build_system_instruction(current_date)
    response_schema = WebSearchResponse.model_json_schema()

    grounding_tool = types.Tool(google_search=types.GoogleSearch())

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[grounding_tool],
        response_mime_type="application/json",
        response_json_schema=response_schema,
        temperature=0.7,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    queries = [
        "Weather in Madrid for next week",
        "Quien es el presidente de España?",
        "Top 5 restaurants in Valencia",
        "When was Python programming language created?",
        "Compare iPhone 15 vs Samsung S24",
    ]

    results = []
    for query in queries:
        start = time.time()
        response = client.models.generate_content(
            model=model_name,
            contents=[types.Content(role="user", parts=[types.Part(text=query)])],
            config=config,
        )
        duration_ms = int((time.time() - start) * 1000)
        raw_text = extract_text(response)
        parsed = None
        parse_error = None
        if raw_text:
            try:
                parsed = WebSearchResponse.model_validate_json(raw_text).model_dump()
            except Exception as exc:
                parse_error = str(exc)

        result_entry = {
            "query": query,
            "duration_ms": duration_ms,
            "response": parsed,
            "has_rich_content": bool(parsed and parsed.get("rich_content")),
        }
        if parse_error:
            result_entry["raw_text"] = raw_text
            result_entry["parse_error"] = parse_error

        results.append(result_entry)

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "results": results,
    }

    output_path = Path("reports") / "websearch_poc_results.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Saved results to {output_path}")


if __name__ == "__main__":
    main()