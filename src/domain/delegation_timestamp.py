"""The timestamp AgentCoordinator.handle_delegation() prepends to every delegated query.

Format: ``[Fri 13 Nov 2026, HH:MM UTC] `` (legacy ``[Nov 13, HH:MM UTC] `` still stripped) — the timezone is always the literal "UTC". An agent that
re-stamps the text itself (Smart's _inject_timestamps) or embeds it strips it first.
"""
import re

DELEGATION_TIMESTAMP_PREFIX = re.compile(
    r"^\[(?:[A-Za-z]{3} \d{2} [A-Za-z]{3} \d{4}|[A-Za-z]{3} \d{2}), \d{2}:\d{2} UTC\] "
)


def strip_delegation_timestamp(text: str) -> str:
    return DELEGATION_TIMESTAMP_PREFIX.sub("", text, count=1)
