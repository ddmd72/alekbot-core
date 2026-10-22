"""search_files_in_drive on FILE_MANAGEMENT (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §10.4, §17.7).

The delegate tool keeps the FIRST description seen per context field name
(BaseAgent._build_delegate_tool_declaration), so a field shared with another intent under another
description would silently lose ours. The new fields are named so no other intent uses them, and this
file fails the moment one does.
"""
from src.domain import drive_tool_texts as tt
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, DRIVE_SPECIALIST, Intent


def test_filter_only_guidance_is_where_the_model_reads_it():
    """Retargeted at the cut: the filter-only rule is in the specialist's `search_drive.query` and, with today's
    intent sentence, in its PROTOCOL_DRIVE_SEARCH; a card's ref feeds the open tool; the before-saying-not-there rule
    is in Smart's one drive intent (DRIVE_SPECIALIST_RFC §4.1, §7.6, §8). Re-review: "EMPTY" restored (today's text)."""
    from pathlib import Path
    query = tt.QUERY
    assert "EMPTY" in query and "filter-only" in query
    token = (Path(__file__).resolve().parents[3] / "prompts_snapshot" / "tokens" / "system"
             / "PROTOCOL_DRIVE_SEARCH.groovy").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    assert "leave query empty" in token and "from a card" in tt.OPEN_REF
    text = DRIVE_SPECIALIST.capability_descriptions[Intent.WORK_WITH_DRIVE_FILES]
    assert "before saying a file is not there" in text


def test_kind_description_names_the_families():
    text = tt.KIND   # the specialist's `search_drive.kind`
    for family in ("spreadsheet", "document", "presentation", "photo"):
        assert family in text


# Names that already collide with differing descriptions (§10.4). Each is a silent
# "first description wins" today; fix one, then remove it here. A new name never joins this list.
KNOWN_COLLISIONS = frozenset({
    "file_ref",     # TODO: nine intents describe it differently
    "resolution",   # TODO: image and video generation
})


def _colliding_names(descriptors):
    seen = {}
    for d in descriptors:
        for schema in d.context_schemas.values():
            for name, spec in schema.items():
                seen.setdefault(name, set()).add(repr(spec))
    return {name for name, specs in seen.items() if len(specs) > 1}


def test_no_new_field_name_collides_anywhere():
    """§10.4 / §17.7: a shared field name with differing descriptions fails, except the known three."""
    assert _colliding_names(ALL_DESCRIPTORS) - KNOWN_COLLISIONS == set()


def test_the_allowlist_only_holds_live_collisions():
    """A fixed collision must leave the list, so it cannot quietly come back."""
    assert KNOWN_COLLISIONS <= _colliding_names(ALL_DESCRIPTORS)
