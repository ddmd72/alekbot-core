"""main.py must hand UserNotificationService to UserAgentFactory. Without it NotesAgent's
confirmations were silently dead from 2026-03-22, and Lelik cannot resolve the channel."""
import ast
from pathlib import Path


def test_main_passes_notification_service_to_the_agent_factory():
    tree = ast.parse(Path("main.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "UserAgentFactory"]
    assert len(calls) == 1
    assert "notification_service" in {k.arg for k in calls[0].keywords}
