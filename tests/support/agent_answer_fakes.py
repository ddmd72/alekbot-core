"""Shared fakes for the orchestrator answer tests (Quick, Smart, Tutor)."""

import logging
from typing import Any, Dict, List, Optional

from src.infrastructure.delegation_engine import DelegationResult

ENVELOPE = {
    "full_response": "Here is the answer.",
    "response_summary": "Short summary.",
    "rich_content": {
        "type": "table",
        "data": {"rows": [{"cells": ["a", "b"]}]},
        "fallback": "a | b",
    },
    "link_list": [{"anchor": "1", "title": "Docs", "url": "https://example.com/docs"}],
}


class FakeEngine:
    """Stands in for DelegationEngine: records execute() kwargs, returns a canned result."""

    def __init__(self, delegation: DelegationResult):
        self._delegation = delegation
        self.last_kwargs: Dict[str, Any] = {}

    def __call__(self, coordinator: Any) -> "FakeEngine":
        return self

    async def execute(self, **kwargs: Any) -> DelegationResult:
        self.last_kwargs = kwargs
        return self._delegation


def result(text: str = "", args: Optional[dict] = None, **kw: Any) -> DelegationResult:
    return DelegationResult(text=text, total_tokens=7, terminal_tool_args=args, **kw)


def empty_answer_records(caplog: Any) -> List[logging.LogRecord]:
    """ERROR-level `empty_answer` records captured by pytest's caplog."""
    return [
        r for r in caplog.records
        if r.levelno == logging.ERROR and getattr(r, "event", None) == "empty_answer"
    ]
