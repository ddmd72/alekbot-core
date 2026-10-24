"""The provider-neutral interactive message (Facts S4 · T5a, plan §3.20): what it renders, how a digit picks an option, the
binding's token rule and the shapes it refuses."""
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import InteractiveChoice
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from src.domain.messaging import MessageRef

LABELS = [("still_true", "yes"), ("ended", "no longer"), ("never", "never"), ("dont_know", "don't remember")]


def options(pairs=LABELS):
    return [InteractiveOption(id=i, label=label) for i, label in pairs]


def message(**over):
    fields = dict(
        prompt="Is this still true?",
        quote="Lives in Lisbon",
        options=options(),
        binding=InteractiveBinding(kind="fact_question", id="q1"),
        reply_hint="Reply with a digit.",
    )
    fields.update(over)
    return InteractiveMessage(**fields)


# ------------------------------------------------------------------------------------------------ plain text

def test_plain_text_is_the_prompt_the_quote_and_the_numbered_options_on_one_line():
    assert message().as_plain_text() == (
        "Is this still true?\n\n«Lives in Lisbon»\n\n1 — yes  2 — no longer  3 — never  4 — don't remember"
    )


def test_plain_text_without_a_quote_has_no_empty_guillemets():
    text = message(quote="  ").as_plain_text()
    assert "«" not in text
    assert text == "Is this still true?\n\n1 — yes  2 — no longer  3 — never  4 — don't remember"


def test_plain_text_quotes_the_subject_as_stored_and_never_adds_the_reply_hint():
    text = message(quote="Живе в Лісабоні — «стара адреса»").as_plain_text()
    assert "«Живе в Лісабоні — «стара адреса»»" in text
    assert "Reply with a digit." not in text


# ------------------------------------------------------------------------------------------------ digits

@pytest.mark.parametrize("reply,expected", [
    ("1", "still_true"), ("2", "ended"), ("3", "never"), ("4", "dont_know"),
    (" 3 ", "never"), ("\n2\n", "ended"),
])
def test_a_whole_reply_that_is_an_options_digit_picks_it(reply, expected):
    assert message().option_for_reply(reply) == expected


@pytest.mark.parametrize("reply", [
    "", "   ", "5", "0", "-1", "1.", "1 2", "12", "yes", "1 — yes", "one", "٣", "１", "1\n2",
])
def test_anything_else_picks_nothing(reply):
    assert message().option_for_reply(reply) is None


def test_the_digits_follow_the_order_of_the_options_whatever_their_ids():
    reordered = message(options=options([("never", "x"), ("still_true", "y")]))
    assert reordered.option_for_reply("1") == "never"
    assert reordered.option_for_reply("2") == "still_true"
    assert reordered.option_for_reply("3") is None


def test_option_looks_up_by_id():
    assert message().option("ended").label == "no longer"
    assert message().option("missing") is None


# ------------------------------------------------------------------------------------------------ binding

def test_the_token_round_trips_and_ids_may_carry_a_colon():
    for ident in ("q1", "u:123:abc", "a.b-c_d"):
        binding = InteractiveBinding(kind="fact_question", id=ident)
        assert binding.token() == f"fact_question:{ident}"
        assert InteractiveBinding.parse(binding.token()) == binding


@pytest.mark.parametrize("token", [
    "", "fact_question", ":q1", "fact_question:", "Fact_Question:q1", "1kind:q1", "fact question:q1",
    "fact_question:q 1", "fact_question:q\n1", "fact_question:" + "x" * 201, "k" * 41 + ":q1",
])
def test_a_token_that_is_not_ours_parses_to_nothing(token):
    assert InteractiveBinding.parse(token) is None


def test_the_longest_binding_fits_a_255_character_field():
    longest = InteractiveBinding(kind="k" * 40, id="x" * 200)
    assert len(longest.token()) <= 255
    assert InteractiveBinding.parse(longest.token()) == longest


def test_a_binding_survives_the_task_payload_encoder():
    from src.adapters.gcp_task_queue import _DomainEncoder
    binding = InteractiveBinding(kind="fact_question", id="q1")
    wire = json.loads(json.dumps({"binding": binding}, cls=_DomainEncoder))
    assert InteractiveBinding(**wire["binding"]) == binding


# ------------------------------------------------------------------------------------------------ refusals

def test_two_to_five_options_are_accepted_and_one_or_six_are_not():
    for count in (2, 5):
        message(options=options([(f"o{i}", f"label {i}") for i in range(count)]))
    for count in (0, 1, 6):
        with pytest.raises(ValidationError):
            message(options=options([(f"o{i}", f"label {i}") for i in range(count)]))


def test_option_ids_must_be_unique():
    with pytest.raises(ValidationError):
        message(options=options([("a", "one"), ("a", "two")]))


@pytest.mark.parametrize("label", ["", "   ", "two\nlines"])
def test_an_option_label_is_one_non_empty_line(label):
    with pytest.raises(ValidationError):
        InteractiveOption(id="a", label=label)


@pytest.mark.parametrize("ident", ["", "has space", "a:b", "x" * 65, "ü"])
def test_an_option_id_is_a_short_token(ident):
    with pytest.raises(ValidationError):
        InteractiveOption(id=ident, label="ok")


def test_a_prompt_is_required():
    with pytest.raises(ValidationError):
        message(prompt="  ")


def test_the_model_is_immutable():
    with pytest.raises(ValidationError):
        message().prompt = "changed"
    with pytest.raises(ValidationError):
        options()[0].label = "changed"


# ------------------------------------------------------------------------------------------------ outcome and choice

def test_an_outcome_shows_the_chosen_option_or_a_note_and_never_neither():
    msg = message()
    assert ChoiceOutcome(message=msg, chosen_option_id="never").chosen_option.label == "never"
    closed = ChoiceOutcome(message=msg, note="Closed.")
    assert closed.chosen_option is None
    with pytest.raises(ValidationError):
        ChoiceOutcome(message=msg)
    with pytest.raises(ValidationError):
        ChoiceOutcome(message=msg, note="   ")
    with pytest.raises(ValidationError):
        ChoiceOutcome(message=msg, chosen_option_id="not_an_option")


def test_a_choice_travels_in_a_task_payload_and_comes_back_equal():
    choice = InteractiveChoice(
        binding=InteractiveBinding(kind="fact_question", id="q1"),
        option_id="ended",
        platform="slack",
        platform_user_id="U123",
        message_ref=MessageRef(platform="slack", channel_id="D1", message_id="1700000000.000100"),
        at=datetime(2026, 10, 24, 9, 30, tzinfo=timezone.utc),
    )
    wire = json.loads(json.dumps(choice.model_dump(mode="json")))
    assert InteractiveChoice.model_validate(wire) == choice


@pytest.mark.parametrize("field", ["option_id", "platform", "platform_user_id"])
def test_a_choice_names_its_option_platform_and_clicker(field):
    fields = dict(
        binding=InteractiveBinding(kind="fact_question", id="q1"), option_id="a", platform="slack",
        platform_user_id="U1", message_ref=MessageRef("slack", "D1", "1"), at=datetime(2026, 10, 24, tzinfo=timezone.utc),
    )
    fields[field] = ""
    with pytest.raises(ValidationError):
        InteractiveChoice(**fields)
