"""The pure helpers and the group logic of the subscription re-card script (slice 3): fences, ids, groups of three, the
three-mail user message, the child's environment, the retry of a call whose answer is unusable, the `--out` guard and
resume. No process is started and no model is called: the call is a fake. Invented cast."""
import json
from pathlib import Path

import pytest

from scripts.email import recard_subscription as rs
from src.domain.email_card_input import build_card_input
from src.domain.email_card_prompt import card_task_line, render_card_static_block
from src.domain.untrusted_text import EXTERNAL_REMINDER
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, answer, mail


def _inputs(ids):
    return [(i, build_card_input(mail(email_id=i), BOOKING_BODY, [], [], direct=True, nonce=NONCE).text) for i in ids]


def _answer_for(ids, **overrides):
    return json.dumps({"cards": [{"email_id": i, **answer(**overrides)} for i in ids]})


def test_fences_are_stripped_with_and_without_the_json_tag():
    assert rs.strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert rs.strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert rs.strip_fences('  {"a": 1}  ') == '{"a": 1}'


def test_cards_are_matched_by_id_in_any_order_unknown_ids_are_kept_out_by_the_caller_and_missing_ones_reported():
    got = rs.cards_by_id({"cards": [{"email_id": "c", "x": 1}, {"email_id": "a", "x": 2}, {"x": 3}, "junk"]})
    assert set(got) == {"a", "c"} and got["a"]["x"] == 2
    assert rs.missing_ids(["a", "b", "c"], got) == ["b"]
    assert rs.cards_by_id({"cards": "no"}) == {} and rs.cards_by_id([1]) == {}


def test_seven_ids_make_two_triples_and_a_single():
    assert rs.triples(list("abcdefg")) == [("a", "b", "c"), ("d", "e", "f"), ("g",)]
    assert rs.triples(list("abcde")) == [("a", "b", "c"), ("d",), ("e",)]
    assert rs.triples([]) == []


def test_the_three_mail_message_holds_the_reminder_once_and_the_blocks_in_order():
    ids = ["m1", "m2", "m3"]
    text = rs.x3_user_message(_inputs(ids))
    assert text.count(EXTERNAL_REMINDER) == 1 and text.startswith(EXTERNAL_REMINDER)
    positions = [text.index(f'<mail id="{i}">') for i in ids]
    assert positions == sorted(positions) and text.count("<mail id=") == 3
    assert text.endswith("Write one card for each of the three mails above, in their order, as the output format says." +
                         card_task_line()[len("Write the card of this mail as the output format says."):])


def test_a_card_input_without_the_reminder_is_refused():
    with pytest.raises(ValueError):
        rs.x3_user_message([("m1", "<mail/>")])


def test_the_three_mail_schema_adds_the_id_first_and_keeps_the_rest():
    item = rs.x3_schema()["properties"]["cards"]["items"]
    assert list(item["properties"])[0] == "email_id" and item["required"][0] == "email_id"
    assert "language" in item["properties"] and rs.x3_schema()["required"] == ["cards"]


def test_the_systems_render_every_placeholder_and_end_with_the_schema():
    block = render_card_static_block(PEOPLE, "alex@example.com")
    for system in (rs.x3_system(block), rs.x1_system(block)):
        assert "{DOC_TYPES}" not in system and block in system and "The JSON must satisfy this schema:" in system
    assert "three mails" in rs.x3_system(block) and "three mails" not in rs.x1_system(block)


def test_thinking_is_on_by_default_and_off_sets_the_child_variable(monkeypatch):
    monkeypatch.setenv("MAX_THINKING_TOKENS", "5000")
    assert "MAX_THINKING_TOKENS" not in rs.child_env("on")
    assert rs.child_env("off")["MAX_THINKING_TOKENS"] == "0"
    assert rs.child_env("on")["NO_PROXY"] == "*" and rs.child_env("off")["no_proxy"] == "*"


def test_the_command_has_no_tools_and_no_session():
    cmd = rs.claude_command("SYSTEM")
    assert cmd[:3] == ["claude", "-p", "--model"] and cmd[cmd.index("--tools") + 1] == "" and "--no-session-persistence" in cmd
    assert cmd[-2:] == ["--system-prompt", "SYSTEM"]


def test_the_result_text_of_a_call_and_an_error_call():
    assert rs.answer_text(json.dumps({"result": "hi", "is_error": False})) == "hi"
    assert rs.answer_text(json.dumps({"result": "x", "is_error": True})) is None
    assert rs.answer_text("not json") is None


class FakeCall:
    def __init__(self, *texts):
        self.texts, self.calls = list(texts), []

    async def __call__(self, system, user):
        self.calls.append((system, user))
        return self.texts.pop(0) if self.texts else None


async def test_a_triple_answered_in_another_order_with_fences_is_matched_by_id():
    group = _inputs(["m1", "m2", "m3"])
    reversed_cards = json.dumps({"cards": json.loads(_answer_for(["m3", "m1", "m2"]))["cards"]})
    call = FakeCall(f"```json\n{reversed_cards}\n```")
    got = await rs.write_group(call, "X3", "X1", group)
    assert set(got) == {"m1", "m2", "m3"} and len(call.calls) == 1


async def test_an_unusable_answer_is_retried_once_as_a_whole_and_the_valid_cards_of_both_are_kept():
    group = _inputs(["m1", "m2", "m3"])
    first = _answer_for(["m1", "m2"])                      # m3 missing
    second = _answer_for(["m1", "m2", "m3"])
    call = FakeCall(first, second)
    got = await rs.write_group(call, "X3", "X1", group)
    assert set(got) == {"m1", "m2", "m3"} and len(call.calls) == 2 and call.calls[0] == call.calls[1]
    assert call.calls[0][0] == "X3"


async def test_a_mail_still_missing_after_the_retry_stays_missing_and_is_never_guessed():
    group = _inputs(["m1", "m2", "m3"])
    call = FakeCall("not json at all", _answer_for(["m1", "m3"]))
    got = await rs.write_group(call, "X3", "X1", group)
    assert set(got) == {"m1", "m3"} and len(call.calls) == 2


async def test_a_card_the_parser_rejects_counts_as_missing():
    group = _inputs(["m1", "m2", "m3"])
    broken = json.loads(_answer_for(["m1", "m2", "m3"]))
    del broken["cards"][1]["doc_type"]
    got = await rs.write_group(FakeCall(json.dumps(broken), json.dumps(broken)), "X3", "X1", group)
    assert set(got) == {"m1", "m3"}


async def test_a_single_mail_uses_the_single_system_and_a_bare_card():
    group = _inputs(["m9"])
    call = FakeCall(json.dumps(answer()))
    got = await rs.write_group(call, "X3", "X1", group)
    assert set(got) == {"m9"} and call.calls[0][0] == "X1" and call.calls[0][1].endswith(card_task_line())


async def test_a_failed_call_twice_gives_nothing():
    assert await rs.write_group(FakeCall(None, None), "X3", "X1", _inputs(["m9"])) == {}


def test_out_must_be_under_scripts_memory_and_resume_skips_carded_ids_only(tmp_path):
    assert rs.under_scripts_memory(rs.REPO / "scripts" / "memory" / "email_cards" / "run.jsonl")
    assert not rs.under_scripts_memory(tmp_path / "run.jsonl")
    assert not rs.under_scripts_memory(rs.REPO / "scripts" / "memory" / ".." / "run.jsonl")
    out = tmp_path / "out.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in (
        {"email_id": "ok", "document": {"card": 1}}, {"email_id": "failed", "document": None})) + "\nbroken line\n")
    assert rs.done_ids(out) == {"ok"} and rs.done_ids(tmp_path / "none.jsonl") == set()
