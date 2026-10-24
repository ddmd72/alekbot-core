"""The Slack codec of an InteractiveBinding (Facts S4 · T5c, L7): block id out, binding back."""
import pytest

from src.adapters.slack.binding_codec import MAX_BLOCK_ID_CHARS, decode, encode
from src.domain.interactive_binding import InteractiveBinding


@pytest.mark.parametrize("ident", ["q1", "u:123:abc", "a.b-c_d", "3f2b8c1e-0d4a-4e57-9a31-6c5f1d2e7b90"])
def test_a_binding_round_trips_and_the_id_may_carry_colons(ident):
    binding = InteractiveBinding(kind="fact_question", id=ident)
    assert encode(binding) == f"fact_question:{ident}"
    assert decode(encode(binding)) == binding


@pytest.mark.parametrize("block_id", [
    "", "fact_question", ":q1", "fact_question:", "Fact_Question:q1", "1kind:q1", "fact question:q1",
    "fact_question:q 1", "fact_question:q\n1", "fact_question:" + "x" * 129, "k" * 41 + ":q1",
])
def test_a_block_id_that_is_not_ours_decodes_to_nothing(block_id):
    assert decode(block_id) is None


def test_the_longest_binding_the_domain_allows_fits_a_block_id():
    longest = InteractiveBinding(kind="k" * 40, id="x" * 128)
    assert len(encode(longest)) <= MAX_BLOCK_ID_CHARS
    assert decode(encode(longest)) == longest


def test_encode_refuses_what_slack_would_refuse(monkeypatch):
    monkeypatch.setattr("src.adapters.slack.binding_codec.MAX_BLOCK_ID_CHARS", 10)
    with pytest.raises(ValueError, match="block id"):
        encode(InteractiveBinding(kind="fact_question", id="q1"))
