"""The main service must hand UserNotificationService to UserAgentFactory. Without it NotesAgent's
confirmations were silently dead from 2026-03-22, and Lelik cannot resolve the channel.

The factory is built by `build_application_graph` (P9 T2), which `main()` calls; both files are read so that a
second construction in either one still fails."""
import ast
from pathlib import Path

_SOURCES = ("main.py", "src/composition/application_graph.py")


def test_main_passes_notification_service_to_the_agent_factory():
    calls = [n for source in _SOURCES for n in ast.walk(ast.parse(Path(source).read_text()))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "UserAgentFactory"]
    assert len(calls) == 1
    assert "notification_service" in {k.arg for k in calls[0].keywords}
