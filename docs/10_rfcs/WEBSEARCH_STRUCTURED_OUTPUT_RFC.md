# WebSearch Structured Output + Grounding (Gemini 3) RFC

**Status:** Draft (POC Completed)  
**Date:** 2026-02-04  
**Owner:** AI Assistant  
**Scope:** WebSearchAgent (non-legacy)

---

## 1. Problem Statement

Web search responses are unstructured and difficult to consume reliably. We need:

- **Consistent JSON output** with fixed fields.
- **Always English** responses (regardless of query language).
- **Optional table/list data** for naturally structured results.
- **Compact history summary** (≤150 chars) for storage/analytics.

---

## 2. Solution Overview

Use **Gemini 3 Structured Outputs + Google Search grounding**:

- Model: `gemini-3-flash-preview`
- Grounding tool: `google_search`
- Structured output enforced via `response_mime_type=application/json` + `response_json_schema`
- Response validated with **Pydantic**
- Automatic function calling disabled (`disable=True`) to avoid tool-call interference

This combination is explicitly supported by Google Gemini API docs (structured outputs with tools).

---

## 3. POC Reference

- **Code:** `scripts/prompt/test_websearch_poc.py`
- **Results:** `reports/websearch_poc_results.json`

The POC runs 5 queries and validates JSON output with the schema below.

---

## 4. JSON Schema (Pydantic)

```python
class RichContent(BaseModel):
    type: str = Field(description="table or list")
    columns: Optional[List[str]] = Field(default=None, description="Column headers")
    rows: Optional[List[List[str]]] = Field(default=None, description="Row data")


class WebSearchResponse(BaseModel):
    full_response: str = Field(description="Complete answer in English")
    history_summary: str = Field(description="Brief factual summary max 150 chars")
    rich_content: Optional[RichContent] = None
```

Schema usage in SDK:

```python
response_schema = WebSearchResponse.model_json_schema()

config = types.GenerateContentConfig(
    system_instruction=system_instruction,
    tools=[grounding_tool],
    response_mime_type="application/json",
    response_json_schema=response_schema,
    temperature=0.7,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)
```

---

## 5. System Instruction (Exact Prompt)

**Used in POC (verbatim):**

```text
You are a Google Search agent.

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

Query: "Weather in Valencia for the week"
{
  "full_response": "*Weather forecast for Valencia:*\n\nMonday-Wednesday sunny 16-19°C. Thursday rain, 14°C. Weekend cooler with clouds.",
  "history_summary": "Valencia weather: Mon-Wed sunny 16-19°C, Thu rain 14°C, weekend cloudy 12-16°C",
  "rich_content": {
    "type": "table",
    "columns": ["Day", "Temp", "Condition", "Humidity", "Wind"],
    "rows": [
      ["Monday", "14-19°C", "☀️ Sunny", "65%", "10 km/h"],
      ["Tuesday", "15-18°C", "⛅ Partly Cloudy", "70%", "12 km/h"]
    ]
  }
}

Query: "Who is president of France?"
{
  "full_response": "The President of France is Emmanuel Macron. He has held this position since May 2017.",
  "history_summary": "President of France: Emmanuel Macron (since May 2017)"
}

Query: "Concerts in Barcelona February"
{
  "full_response": "*Concerts in Barcelona - February 2026:*\n\n*Coldplay* — Feb 10, Palau Sant Jordi\n*Billie Eilish* — Feb 15, Primavera Sound\n*Rosalía* — Feb 22, Sala Apolo\n*The Weeknd* — Feb 28, Estadi Olímpic",
  "history_summary": "Barcelona Feb 2026: Coldplay (10th), Billie Eilish (15th), Rosalía (22nd), The Weeknd (28th)",
  "rich_content": {
    "type": "table",
    "columns": ["Artist", "Date", "Venue"],
    "rows": [
      ["Coldplay", "Feb 10", "Palau Sant Jordi"],
      ["Billie Eilish", "Feb 15", "Primavera Sound"]
    ]
  }
}

Execute search for the user's query.
```

---

## 6. POC Script (Execution)

Run:

```bash
GEMINI_API_KEY=... python scripts/prompt/test_websearch_poc.py
```

Output file:

```
reports/websearch_poc_results.json
```

If parsing fails, raw JSON is stored as fallback:

```json
{
  "raw_text": "...",
  "parse_error": "..."
}
```

---

## 7. Integration Notes (WebSearchAgent)

Target agent: `src/agents/web_search_agent.py` (non-legacy only).

Required updates:

1. **Model**: set `gemini-3-flash-preview`
2. **Config**: use `response_json_schema` + grounding tool
3. **Parsing**: validate output with `WebSearchResponse`
4. **History summary**: always prefer `history_summary` field
5. **Fallback**: if parsing fails, keep raw JSON string for debugging

---

## 8. Acceptance Criteria

- JSON output always conforms to schema
- English output for all queries
- `history_summary` ≤ 150 chars (factual, no “Answered” phrasing)
- `rich_content` appears only for tabular outputs
- POC results reproducible from script

---

## 9. References

- **Google Gemini Structured Outputs + Tools**: https://ai.google.dev/gemini-api/docs/structured-output
- **POC script**: `scripts/prompt/test_websearch_poc.py`
- **POC results**: `reports/websearch_poc_results.json`
