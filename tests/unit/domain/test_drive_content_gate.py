"""A14 / OQ-A1: on a run that is not a chat turn, changes are refused once passages were read in an earlier step."""
import pytest

from src.domain.drive_content_gate import content_gate_applies, write_refused


class TestWhoIsGated:
    def test_a_chat_turn_is_not_gated(self):
        assert content_gate_applies({"interactive_delivery": True, "run_origin": "chat"}) is False

    @pytest.mark.parametrize("context", [
        {"run_origin": "background:reminder"},
        {"run_origin": "background:daily_email_review"},
        {"run_origin": "voice:ask_alek"},
        {"run_origin": "voice:tell_alek"},
        {},                                                   # no origin: fail-closed
        {"interactive_delivery": False, "run_origin": "chat"},
        {"interactive_delivery": None},
    ])
    def test_every_other_run_is_gated(self, context):
        assert content_gate_applies(context) is True


class TestWhenAWriteIsRefused:
    def test_a_change_after_the_passages_step_is_refused(self):
        assert write_refused(gated=True, step=3, passages_step=2) is True

    def test_a_change_co_emitted_with_the_first_passages_read_is_not(self):
        assert write_refused(gated=True, step=2, passages_step=2) is False

    def test_a_change_before_any_passages_is_not(self):
        assert write_refused(gated=True, step=1, passages_step=2) is False

    def test_no_passages_never_refuses(self):
        assert write_refused(gated=True, step=5, passages_step=None) is False

    def test_a_chat_turn_never_refuses(self):
        assert write_refused(gated=False, step=5, passages_step=1) is False
