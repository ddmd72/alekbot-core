"""The tick's catch-all logs the exception class, a digest and the frames, never its message (hardening, §27.12)."""
import pytest

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")

MARKER = "TRANSCRIPT-TEXT-MARKER"


async def test_an_unexpected_error_ends_the_tick_without_its_message_in_the_log(caplog, monkeypatch):
    w = ContentWorld()

    async def boom(*a, **k):
        raise RuntimeError(f"stage failed on {MARKER}.md")
    monkeypatch.setattr(w.svc, "_due_loop", boom)
    w.add_doc("a.md", "some text " * 20)
    result = await w.tick()
    assert result.status == "error"
    line = next(r.getMessage() for r in caplog.records if "tick failed" in r.getMessage())
    assert "RuntimeError (message digest " in line and "boom" in line   # type, digest, frame of the raising code
    assert MARKER not in caplog.text
