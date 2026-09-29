"""The timestamp AgentCoordinator.handle_delegation() prepends to every delegated query.

Format: ``[Mon DD, HH:MM UTC] `` — the timezone is always the literal "UTC". An agent that
re-stamps the text itself (Smart's _inject_timestamps) or embeds it strips it first.
"""
import re

DELEGATION_TIMESTAMP_PREFIX = re.compile(r"^\[[A-Za-z]{3} \d{2}, \d{2}:\d{2} UTC\] ")


def strip_delegation_timestamp(text: str) -> str:
    return DELEGATION_TIMESTAMP_PREFIX.sub("", text, count=1)
