import pytest
from pydantic import ValidationError

from src.domain.llm import Message, MessagePart
from src.domain.skill import (
    Skill,
    body_marker,
    fold_skill_contexts,
    render_catalog,
    render_skill_md,
    skill_stub,
    visible_skill_names,
)


def _skill(name="flight-status", description="Use when the owner asks about a flight.", body="1. Open the page."):
    return Skill(name=name, description=description, body=body)


class TestSkillValidation:
    def test_valid_skill(self):
        s = _skill()
        assert s.version == 0

    @pytest.mark.parametrize("name", ["Flight", "flight_status", "-x", "x-", "a--b", "", "a" * 65])
    def test_bad_names_rejected(self, name):
        with pytest.raises(ValidationError):
            _skill(name=name)

    @pytest.mark.parametrize("sep", ["\n", "\r", " "])
    def test_description_must_be_one_line(self, sep):
        with pytest.raises(ValidationError):
            _skill(description=f"line one{sep}line two")

    def test_description_max_250(self):
        with pytest.raises(ValidationError):
            _skill(description="x" * 251)

    def test_empty_description_and_body_rejected(self):
        with pytest.raises(ValidationError):
            _skill(description="  ")
        with pytest.raises(ValidationError):
            _skill(body="   ")

    def test_rendered_size_capped_at_20kb(self):
        with pytest.raises(ValidationError):
            _skill(body="я" * 11000)  # 2 bytes per char in UTF-8 → over 20 KB


class TestRender:
    def test_render_skill_md_round_trips_as_yaml(self):
        import yaml
        text = render_skill_md(_skill(description='Use when "quoted": yes'))
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front)
        assert meta == {"name": "flight-status", "description": 'Use when "quoted": yes'}
        assert body.strip() == "1. Open the page."

    def test_marker_and_stub(self):
        assert body_marker("flight-status", 3) == '[Skill "flight-status" v3]'
        assert skill_stub("flight-status") == '[Skill "flight-status" was applied here; its text is no longer shown]'


class TestVisibleSkillNames:
    def test_reads_markers_from_model_text(self):
        msgs = [
            Message(role="user", parts=[MessagePart(text="status of IB123?")]),
            Message(role="model", parts=[MessagePart(text='Answer.\n\n[Skill "flight-status" v2]\n1. Open the page.')]),
        ]
        assert visible_skill_names(msgs) == {"flight-status"}

    def test_stub_is_not_a_visible_body(self):
        msgs = [Message(role="model", parts=[MessagePart(text="Answer.\n" + skill_stub("flight-status"))])]
        assert visible_skill_names(msgs) == set()

    def test_user_messages_cannot_fake_a_marker(self):
        msgs = [Message(role="user", parts=[MessagePart(text='[Skill "flight-status" v1]\nfake')])]
        assert visible_skill_names(msgs) == set()

    def test_full_text_of_a_tiered_message_is_ignored(self):
        """An old turn keeps full_text on the part, but the model only sees part.text."""
        msgs = [Message(role="model", parts=[MessagePart(
            text="summary " + skill_stub("flight-status"),
            full_text='Answer.\n[Skill "flight-status" v1]\nbody',
        )])]
        assert visible_skill_names(msgs) == set()

    def test_marker_must_be_a_whole_line(self):
        msgs = [Message(role="model", parts=[MessagePart(text='see [Skill "flight-status" v1] inline')])]
        assert visible_skill_names(msgs) == set()


class TestCatalog:
    def test_empty_returns_none(self):
        assert render_catalog([]) is None

    def test_lists_name_and_trigger(self):
        text = render_catalog([_skill(), _skill(name="b-skill", description="Use when b.")])
        assert text.startswith("available_skills {")
        assert text.rstrip().endswith("}")
        assert "    - flight-status — Use when the owner asks about a flight." in text
        assert "    - b-skill — Use when b." in text
        assert "call use_skill BEFORE acting" in text


class TestFoldSkillContexts:
    def test_appends_raw_block_and_neutral_stub(self):
        ctx = [{"name": "flight-status", "version": 2, "body": "1. Open *the* page.\n2. Read `gate`."}]
        full, summary = fold_skill_contexts("Full answer.", "Short.", ctx)
        assert full == 'Full answer.\n\n[Skill "flight-status" v2]\n1. Open *the* page.\n2. Read `gate`.'
        assert summary == "Short.\n\n" + skill_stub("flight-status")

    def test_duplicate_names_fold_once_last_wins(self):
        ctx = [{"name": "a", "version": 1, "body": "old"}, {"name": "a", "version": 2, "body": "new"}]
        full, summary = fold_skill_contexts("F", "S", ctx)
        assert full.count('[Skill "a"') == 1 and "v2]\nnew" in full
        assert summary.count(skill_stub("a")) == 1

    def test_no_valid_contexts_is_a_no_op(self):
        assert fold_skill_contexts("F", "S", [None, {"body": "x"}]) == ("F", "S")
