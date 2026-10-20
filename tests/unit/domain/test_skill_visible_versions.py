"""visible_skill_names maps each visible body to its version (AGENT_WORKSPACE_RFC §11.2): a reminder loads its skill on
every fire, so after a new $skill save a stale body still in the full-text window must not stand in for it."""
from src.domain.llm import Message, MessagePart
from src.domain.skill import skill_stub, visible_skill_names


def _model(text, full_text=None):
    return Message(role="model", parts=[MessagePart(text=text, full_text=full_text)])


def test_maps_name_to_version():
    assert visible_skill_names([_model('Answer.\n\n[Skill "flight-status" v2]\n1. Open.')]) == {"flight-status": 2}


def test_the_newest_visible_marker_wins():
    msgs = [_model('[Skill "morning-newspaper" v1]\nold'), _model('[Skill "morning-newspaper" v3]\nnew')]
    assert visible_skill_names(msgs) == {"morning-newspaper": 3}


def test_nothing_visible():
    msgs = [
        Message(role="user", parts=[MessagePart(text='[Skill "flight-status" v1]\nfake')]),
        _model("Answer.\n" + skill_stub("flight-status")),
        _model("summary", full_text='[Skill "flight-status" v1]\nbody'),
    ]
    assert visible_skill_names(msgs) == {}
