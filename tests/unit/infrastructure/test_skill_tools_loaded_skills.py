"""The shared `LoadedSkills` set (DRIVE_FILE_NOTES_RFC §4.5 layer 3): use_skill records what it loads in this
execution, so another handler's skill gate sees it; a body visible in history counts at any version; without a
shared set use_skill behaves exactly as before."""
from src.domain.llm import ToolCall
from src.domain.skill import Skill
from src.infrastructure.skill_tools import LoadedSkills, make_use_skill_handler

NOTES = Skill(name="drive-file-notes", description="Use before annotate_drive_file.", body="1. Ask.", version=2)
OTHER = Skill(name="flight-status", description="Use when a flight is asked about.", body="1. Open.", version=1)


def _call(name: str) -> ToolCall:
    return ToolCall(name="use_skill", args={"name": name})


def test_a_skill_visible_in_history_counts_at_any_version():
    loaded = LoadedSkills({"drive-file-notes": 1})
    assert "drive-file-notes" in loaded
    assert "flight-status" not in loaded


async def test_use_skill_records_what_it_loads_in_the_shared_set():
    loaded = LoadedSkills({})
    handle = make_use_skill_handler([NOTES, OTHER], {}, loaded)
    assert "drive-file-notes" not in loaded
    result = await handle(_call("drive-file-notes"))
    assert not result.failed
    assert "drive-file-notes" in loaded and loaded.now == {"drive-file-notes"}


async def test_a_miss_records_nothing():
    loaded = LoadedSkills()
    handle = make_use_skill_handler([OTHER], {}, loaded)
    result = await handle(_call("drive-file-notes"))
    assert result.failed and "drive-file-notes" not in loaded


async def test_a_stale_visible_body_still_reloads_and_then_short_circuits():
    loaded = LoadedSkills({"drive-file-notes": 1})
    handle = make_use_skill_handler([NOTES], loaded.visible, loaded)
    first = await handle(_call("drive-file-notes"))
    assert "1. Ask." in first.result_str
    second = await handle(_call("drive-file-notes"))
    assert "already in your context" in second.result_str


async def test_without_a_shared_set_the_handler_keeps_its_own():
    handle = make_use_skill_handler([NOTES], {})
    await handle(_call("drive-file-notes"))
    again = await handle(_call("drive-file-notes"))
    assert "already in your context" in again.result_str
