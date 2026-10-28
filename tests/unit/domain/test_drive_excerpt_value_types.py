"""The passages value types (USER_DRIVE_INDEXING_RFC §28.3 A6, A10): what selection and the render hand on."""
import dataclasses

import pytest

from src.domain.drive_excerpt_context_plan import ContextPlan
from src.domain.drive_excerpt_rendered import RenderedExcerpts
from src.domain.drive_excerpt_selection import Selection
from src.domain.drive_excerpts import select_excerpts


class TestSelection:
    def test_empty_by_default(self):
        sel = Selection()
        assert (sel.chunks, sel.repeated, dict(sel.also_at)) == ((), (), {})

    def test_two_defaults_do_not_share_their_copies_map(self):
        assert Selection().also_at is not Selection().also_at

    def test_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            Selection().chunks = (("item-1", 1, 0),)  # type: ignore[misc]

    def test_select_excerpts_fills_every_field(self):
        ranked = [("item-1", 1, 0), ("item-2", 4, 0), ("item-1", 1, 1)]
        sel = select_excerpts(ranked, {cid: 100 for cid in ranked}, shown=frozenset({("item-1", 1, 1)}),
                              hashes={"item-1": "sha:abc", "item-2": "sha:abc"})
        assert sel == Selection(chunks=(("item-1", 1, 0),), repeated=(("item-1", 1, 1),),
                                also_at={"item-1": ("item-2",)})


class TestRenderedExcerpts:
    def test_holds_the_text_and_the_plan_it_printed(self):
        plan = ContextPlan(described=frozenset({"item-1"}), shown_after=frozenset({"f-rd"}), has_owner_line=True)
        rendered = RenderedExcerpts(text="Passages nearest to the question", plan=plan)
        assert rendered.text == "Passages nearest to the question" and rendered.plan is plan
        assert [f.name for f in dataclasses.fields(RenderedExcerpts)] == ["text", "plan"]

    def test_immutable(self):
        rendered = RenderedExcerpts(text="", plan=ContextPlan.EMPTY)
        with pytest.raises(dataclasses.FrozenInstanceError):
            rendered.text = "changed"  # type: ignore[misc]
