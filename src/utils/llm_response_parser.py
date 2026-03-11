"""
Universal LLM response parser.

Provides centralized logic for parsing LLM outputs that may be:
1. Plain text (legacy/fallback)
2. JSON with structured fields (full_response, history_summary, rich_content)

This utility ensures all user-facing agents (Quick, Smart) handle
LLM responses consistently.
"""

import json
import re
from typing import Tuple, Optional
from ..domain.messaging import RichContent


def _repair_unescaped_quotes(text: str) -> Optional[str]:
    """
    Attempt to repair a JSON string that contains unescaped double quotes
    inside string values. Uses a state machine to detect quote boundaries:
    a quote is treated as a closing delimiter only when the next non-whitespace
    character is one of :  ,  }  ] (i.e., a valid JSON structural character).
    Otherwise the quote is escaped.

    Limitation: cannot repair cases where an unescaped inner quote is followed
    immediately by one of those structural characters (ambiguous by definition).
    Returns None if the input is clearly not repairable.
    """
    result = []
    i = 0
    n = len(text)
    in_string = False
    escape_next = False

    while i < n:
        ch = text[i]

        if escape_next:
            result.append(ch)
            escape_next = False
            i += 1
            continue

        if ch == "\\":
            result.append(ch)
            escape_next = True
            i += 1
            continue

        if ch == '"':
            if not in_string:
                in_string = True
                result.append(ch)
            else:
                # Look ahead past whitespace to determine if this is a closing quote.
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                if j >= n or text[j] in ":,}]":
                    in_string = False
                    result.append(ch)
                else:
                    # Unescaped quote inside a string value — escape it.
                    result.append('\\"')
        else:
            result.append(ch)

        i += 1

    return "".join(result)


def _load_json(candidate: str) -> Optional[dict]:
    """Try json.loads; on failure attempt quote repair and retry."""
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass

    repaired = _repair_unescaped_quotes(candidate)
    try:
        return json.loads(repaired)
    except (json.JSONDecodeError, ValueError):
        return None


def parse_llm_response(raw_text: str) -> Tuple[Optional[str], Optional[str], Optional[RichContent], list]:
    """
    Parse raw LLM output into user message, history summary, rich content, and link list.

    Expected JSON structure:
    {
        "full_response": "Detailed text for user...",
        "response_summary": "Concise summary for history...",
        "rich_content": { "type": "...", "data": {...}, "fallback": "..." },
        "link_list": [{"anchor": "1", "title": "Place Name", "url": "https://..."}]
    }

    Returns:
        Tuple of (user_text, history_summary, rich_content, link_list).
        Falls back to (raw_text, None, None, []) when no JSON envelope is found.
    """
    if not raw_text:
        return "", None, None, []

    cleaned_text = raw_text.strip()

    # Locate the JSON envelope via multiple extraction strategies.
    # Each strategy produces a candidate string for _load_json().
    candidate: Optional[str] = None

    # Strategy 1: direct JSON (starts and ends with braces)
    if cleaned_text.startswith("{") and cleaned_text.endswith("}"):
        candidate = cleaned_text

    # Strategy 2: markdown code block wrapping JSON
    if candidate is None:
        json_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned_text, re.DOTALL)
        if json_match:
            candidate = json_match.group(1)

    # Strategy 3: "full_response" anchor extraction.
    # Handles text preamble ("some text\n{...}") and outer-quoted JSON ("\"{\n...}\"").
    # rfind('"full_response"') finds the LAST occurrence — robust against nested JSON in text.
    # rfind('{', 0, idx) skips any preamble text or outer quote character.
    # rfind('}') finds the JSON's closing brace even when a trailing '"' follows.
    if candidate is None:
        marker = '"full_response"'
        idx = cleaned_text.rfind(marker)
        if idx != -1:
            brace = cleaned_text.rfind("{", 0, idx)
            closing = cleaned_text.rfind("}")
            if brace != -1 and closing > brace:
                candidate = cleaned_text[brace : closing + 1]

    if candidate is None:
        return cleaned_text, None, None, []

    data = _load_json(candidate)

    if not isinstance(data, dict):
        return cleaned_text, None, None, []

    if not any(k in data for k in ("full_response", "response_summary", "rich_content")):
        return cleaned_text, None, None, []

    user_text = data.get("full_response")
    history_summary = data.get("response_summary")

    # Normalize literal \n sequences that Claude sometimes double-escapes in JSON output.
    # The model outputs \\n (which json.loads decodes to literal backslash+n instead of newline).
    if user_text:
        user_text = user_text.replace("\\n", "\n")
    if history_summary:
        history_summary = history_summary.replace("\\n", "\n")
    rich_data = data.get("rich_content")

    rich_content = None
    if isinstance(rich_data, dict):
        rich_content = RichContent(
            content_type=rich_data.get("type", "unknown"),
            data=rich_data.get("data", {}),
            fallback_text=rich_data.get("fallback", "")
        )
    elif isinstance(rich_data, list) and rich_data:
        item = rich_data[0]
        if isinstance(item, dict):
            rich_content = RichContent(
                content_type=item.get("type", "unknown"),
                data=item.get("data", {}),
                fallback_text=item.get("fallback", "")
            )

    raw_links = data.get("link_list") or []
    link_list = [
        item for item in raw_links
        if isinstance(item, dict) and "anchor" in item and "title" in item and "url" in item
    ]

    return user_text, history_summary, rich_content, link_list
