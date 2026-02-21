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
from typing import Tuple, Optional, Dict, Any
from ..domain.messaging import RichContent


def parse_llm_response(raw_text: str) -> Tuple[Optional[str], Optional[str], Optional[RichContent]]:
    """
    Parse raw LLM output into user message, history summary, and rich content.
    
    Expected JSON structure:
    {
        "full_response": "Detailed text for user...",
        "history_summary": "Concise summary for history...",
        "rich_content": {
            "type": "weather",
            "data": {...},
            "fallback": "Weather is sunny"
        }
    }
    
    Args:
        raw_text: Raw string output from LLM
        
    Returns:
        Tuple containing:
        - user_text: Text to display to user (can be None if rich-only)
        - history_summary: Text to save in history (can be None)
        - rich_content: Structured UI data (can be None)
    """
    if not raw_text:
        return "", None, None
        
    cleaned_text = raw_text.strip()
    
    # Fast path: if not looking like JSON, treat as plain text
    if not (cleaned_text.startswith("{") and cleaned_text.endswith("}")):
        # Handle markdown code blocks wrapping JSON
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned_text, re.DOTALL)
        if not json_match:
            return cleaned_text, None, None
        cleaned_text = json_match.group(1)
    
    try:
        data = json.loads(cleaned_text)
        
        # Validation: must be a dict
        if not isinstance(data, dict):
            return raw_text, None, None
            
        # Validate that this looks like a response envelope, not an arbitrary JSON snippet
        if not any(k in data for k in ("full_response", "response_summary", "rich_content")):
            return raw_text, None, None

        # Extract fields
        user_text = data.get("full_response")
        history_summary = data.get("response_summary")
        rich_data = data.get("rich_content")
        
        # Validate rich_content structure
        rich_content = None
        if isinstance(rich_data, dict):
            rich_content = RichContent(
                content_type=rich_data.get("type", "unknown"),
                data=rich_data.get("data", {}),
                fallback_text=rich_data.get("fallback", "")
            )
            
        return user_text, history_summary, rich_content
        
    except (json.JSONDecodeError, TypeError):
        # Fallback: treat broken JSON as plain text to avoid losing data
        return raw_text, None, None
