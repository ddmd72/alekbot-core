"""
Universal LLM response parser.

Provides centralized logic for parsing LLM outputs that may be:
1. Plain text (legacy/fallback)
2. JSON with structured fields (full_response, history_summary, rich_content)

This utility ensures all user-facing agents (Quick, Smart) handle
LLM responses consistently.
"""

import json
import logging
import re
from typing import Any, Tuple, Optional
from ..domain.messaging import RichContent
from .logger import logger


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


_ENVELOPE_KEYS = ("full_response", "response_summary", "rich_content")
# Keys whose presence means a terminal tool's args ARE the answer (Grok shape).
_GROK_SHAPE_KEYS = _ENVELOPE_KEYS + ("history_summary", "link_list")

_ParsedTuple = Tuple[Optional[str], Optional[str], Optional[RichContent], list]


def _load_json_traced(candidate: str) -> Tuple[Optional[Any], Optional[str]]:
    """Return (parsed, path) where path is "direct" or "repaired"; (None, None) on failure."""
    try:
        return json.loads(candidate, strict=False), "direct"
    except (json.JSONDecodeError, ValueError):
        logger.debug("Initial JSON parse failed, attempting quote repair")

    repaired = _repair_unescaped_quotes(candidate)
    try:
        return json.loads(repaired, strict=False), "repaired"
    except (json.JSONDecodeError, ValueError):
        return None, None


def _load_json(candidate: str) -> Optional[dict]:
    """Try json.loads; on failure attempt quote repair and retry."""
    return _load_json_traced(candidate)[0]


def _log_parse(source: str, outcome: str, via: Optional[str], length: int, tail: Optional[str] = None) -> None:
    """One structured line per parser call. Never logs the text itself (only a short tail)."""
    msg = f"[LlmResponseParse] source={source} outcome={outcome} via={via} len={length}"
    if tail is not None:
        msg += f" tail={tail!r}"
    level = logging.INFO
    if source == "deliver" and outcome == "unparseable":
        # A non-empty unparseable string still reaches the user as prose; only an empty one is an error.
        level = logging.WARNING if length > 0 else logging.ERROR
    logger.log(
        level,
        msg,
        extra={"event": "llm_response_parse", "source": source, "outcome": outcome, "via": via},
    )


def _envelope_fields(data: dict) -> _ParsedTuple:
    """Map a parsed envelope dict to (user_text, history_summary, rich_content, link_list)."""
    user_text = data.get("full_response")
    history_summary = data.get("response_summary")

    # Normalize literal \n sequences that Claude sometimes double-escapes in JSON output.
    # The model outputs \\n (which json.loads decodes to literal backslash+n instead of newline).
    if user_text and isinstance(user_text, str):
        user_text = user_text.replace("\\n", "\n")
    if history_summary and isinstance(history_summary, str):
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


def parse_llm_response(raw_text: str, *, source: str = "text") -> _ParsedTuple:
    """
    Parse raw LLM output into user message, history summary, rich content, and link list.

    Expected JSON structure:
    {
        "full_response": "Detailed text for user...",
        "response_summary": "Concise summary for history...",
        "rich_content": { "type": "...", "data": {...}, "fallback": "..." },
        "link_list": [{"anchor": "1", "title": "Place Name", "url": "https://..."}]
    }

    `source` labels the single log line this call writes ("text" or "deliver").

    Returns:
        Tuple of (user_text, history_summary, rich_content, link_list).
        Falls back to (raw_text, None, None, []) when no JSON envelope is found.
    """
    if not raw_text:
        _log_parse(source, "unparseable", None, 0)
        return "", None, None, []

    cleaned_text = raw_text.strip()
    length = len(raw_text)

    # Locate the JSON envelope via multiple extraction strategies.
    # Each strategy produces a candidate string for _load_json().
    candidate: Optional[str] = None
    via: Optional[str] = None
    fenced_body: Optional[str] = None

    # Strategy 1: direct JSON (starts and ends with braces)
    if cleaned_text.startswith("{") and cleaned_text.endswith("}"):
        candidate = cleaned_text
        via = "direct"

    # Strategy 2: markdown code block wrapping JSON
    if candidate is None:
        json_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned_text, re.DOTALL)
        if json_match:
            candidate = json_match.group(1)
            fenced_body = candidate
            via = "fence"

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
                via = "anchor"

    if candidate is not None:
        data, path = _load_json_traced(candidate)
        if isinstance(data, dict) and any(k in data for k in _ENVELOPE_KEYS):
            _log_parse(source, "repaired" if path == "repaired" else "parsed", via, length)
            return _envelope_fields(data)

    # Last step, only after the chain above produced nothing usable: the first
    # complete JSON object, ignoring whatever follows it.
    base = fenced_body if fenced_body is not None else cleaned_text
    start = base.find("{")
    if start != -1:
        try:
            obj, end = json.JSONDecoder(strict=False).raw_decode(base, start)
        except ValueError:
            obj, end = None, 0
        if isinstance(obj, dict) and any(k in obj for k in _ENVELOPE_KEYS):
            _log_parse(source, "first_object", None, length, tail=base[end:end + 60])
            return _envelope_fields(obj)

    _log_parse(source, "unparseable", None, length)
    return cleaned_text, None, None, []


def extract_structured_response(
    terminal_tool_args: Optional[dict], text: str,
) -> _ParsedTuple:
    """
    Extract (user_text, history_summary, rich_content, link_list) from a delegation
    result, handling both terminal-tool shapes and schema-enforced JSON text:
    - Claude's deliver_response: args {"response": <str|dict>} (the string is parsed
      by parse_llm_response, source="deliver");
    - Grok's deliver_response: the args ARE the response schema;
    - every other provider: JSON text, parsed by parse_llm_response(text).
    Never raises. If the terminal answer yields no user text but the model wrote text
    beside the call, that text becomes the user text (summary/rich/links stay).
    """
    if not terminal_tool_args:
        return parse_llm_response(text)

    args = terminal_tool_args
    if "response" in args and not any(k in args for k in _GROK_SHAPE_KEYS):
        raw = args["response"]
        if isinstance(raw, str):
            result = parse_llm_response(raw, source="deliver")
        elif isinstance(raw, dict) and any(k in raw for k in _ENVELOPE_KEYS):
            _log_parse("deliver", "parsed", "object", len(json.dumps(raw, default=str)))
            result = _envelope_fields(raw)
        else:
            _log_parse("deliver", "unparseable", None, 0)
            result = ("", None, None, [])
    else:
        user_text = args.get("full_response", "")
        summary = args.get("response_summary") or args.get("history_summary")
        rich_data = args.get("rich_content")
        rich_content = (
            RichContent(
                content_type=rich_data.get("type", "unknown"),
                data=rich_data.get("data", {}),
                fallback_text=rich_data.get("fallback", ""),
            )
            if isinstance(rich_data, dict) else None
        )
        raw_links = args.get("link_list") or []
        link_list = [
            item for item in raw_links
            if isinstance(item, dict) and "anchor" in item and "title" in item and "url" in item
        ]
        result = (user_text, summary, rich_content, link_list)

    user_text, summary, rich_content, link_list = result
    empty = user_text is None or (isinstance(user_text, str) and not user_text.strip())
    if empty and text and text.strip():
        # `or ""`: the beside-text may be an envelope without full_response (None).
        beside = parse_llm_response(text)[0] or ""
        logger.info(
            "[LlmResponseParse] terminal answer empty, using text beside the call",
            extra={"event": "terminal_text_used"},
        )
        return beside, summary, rich_content, link_list
    return result
